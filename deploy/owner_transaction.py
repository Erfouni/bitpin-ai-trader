#!/usr/bin/env python3
"""Serialize owner wallet activation with the panel's complete mutation scope.

Only our fixed root-owned runtime lock is opened. Inherited descriptors share
the parent's open file description; closing a duplicate never explicitly
unlocks the parent's transaction.
"""
import argparse
import contextlib
import math
import os
import stat
import sys
import time

try:
    import fcntl
except ImportError:
    fcntl = None

LOCK_PATH = '/run/bitpin-panel/helper.lock'
FD_ENV = 'BITPIN_OWNER_TRANSACTION_FD'
WALLET_SCRIPT = '/opt/bitpin-bot/deploy/enable_wallet_context.sh'


def _require_runtime():
    if fcntl is None or not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY'):
        raise ValueError('owner transactions require Linux flock and NOFOLLOW')
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        raise ValueError('owner transactions require root')


def _trusted_directory(fd):
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('transaction path directory must be root-owned and not group/world writable')


def _open_regular(path, create=False):
    """Walk every ancestor with NOFOLLOW; only the lock's final parent may be made."""
    _require_runtime()
    if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path:
        raise ValueError('transaction path must be an absolute canonical path')
    components = [part for part in path.split(os.path.sep) if part]
    if len(components) < 2:
        raise ValueError('invalid transaction path')
    parent, fd = os.open(os.path.sep, os.O_RDONLY | os.O_DIRECTORY), None
    try:
        _trusted_directory(parent)
        for index, part in enumerate(components[:-1]):
            created = False
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            except FileNotFoundError:
                if not create or index != len(components) - 2:
                    raise
                try:
                    os.mkdir(part, 0o755, dir_fd=parent)
                    created = True
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
            if created:
                # The owner shell uses umask 077. New runtime directories
                # still need traversal for the panel socket's service user.
                os.fchmod(parent, 0o755)
            _trusted_directory(parent)
        flags = (os.O_RDWR if create else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK
        if create:
            flags |= os.O_CREAT
        fd = os.open(components[-1], flags, 0o600, dir_fd=parent)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                or info.st_nlink != 1):
            raise ValueError('transaction file must be a regular root-owned file without writable or hardlink aliases')
        result, fd = fd, None
        return result
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def open_owner_lock(timeout=120.0):
    """Return an exclusively locked descriptor; the caller owns its close."""
    _require_runtime()
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 3600:
        raise ValueError('invalid transaction lock timeout')
    descriptor = _open_regular(LOCK_PATH, create=True)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return descriptor
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('another owner or panel change is still running')
                time.sleep(min(.1, remaining))
    except BaseException:
        os.close(descriptor)
        raise


def _borrow_inherited(raw):
    _require_runtime()
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal() or not 3 <= int(raw) <= 1048576:
        raise ValueError('invalid inherited transaction descriptor')
    descriptor, expected = None, None
    try:
        descriptor = os.dup(int(raw))
        expected = _open_regular(LOCK_PATH)
        borrowed, current = os.fstat(descriptor), os.fstat(expected)
        if (not stat.S_ISREG(borrowed.st_mode) or borrowed.st_uid != 0 or borrowed.st_mode & 0o022
                or borrowed.st_nlink != 1 or (borrowed.st_dev, borrowed.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError('inherited transaction descriptor does not match the fixed lock')
        # The same open file description already held by our parent reacquires
        # immediately. A competing description cannot impersonate a held lock.
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result, descriptor = descriptor, None
        return result
    except OSError as exc:
        raise ValueError('invalid or unheld inherited transaction descriptor') from exc
    finally:
        if expected is not None:
            os.close(expected)
        if descriptor is not None:
            os.close(descriptor)


@contextlib.contextmanager
def owner_transaction(timeout=120.0):
    """Hold one mutation scope, or borrow the shell's verified inherited lock."""
    raw = os.environ.get(FD_ENV)
    descriptor = _borrow_inherited(raw) if raw is not None else open_owner_lock(timeout)
    try:
        yield descriptor
    finally:
        # LOCK_UN here would release the parent's flock through a borrowed FD.
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--run-wallet-activation', action='store_true')
    action.add_argument('--verify-inherited', action='store_true')
    args = parser.parse_args()
    if args.verify_inherited:
        if FD_ENV not in os.environ:
            raise ValueError('owner shell requires an inherited transaction descriptor')
        with owner_transaction():
            pass
        return 0
    if FD_ENV in os.environ:
        raise ValueError('wallet bootstrap must start outside an inherited transaction')
    with owner_transaction() as descriptor:
        script_fd = _open_regular(WALLET_SCRIPT)
        os.close(script_fd)
        os.set_inheritable(descriptor, True)
        env = dict(os.environ)
        env[FD_ENV] = str(descriptor)
        os.execve('/bin/bash', ['/bin/bash', WALLET_SCRIPT], env)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as exc:
        print('FAILED: %s' % exc, file=sys.stderr)
        sys.exit(1)
