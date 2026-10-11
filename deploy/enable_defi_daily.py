#!/usr/bin/env python3
"""Prepare one explicit context setting. No credentials, account requests or confirmation phrase."""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bitpin.analysis import validate_context_config
from bitpin.brain import check_kimi_config, load_kimi_config
from bitpin.defi_context import load_defi_daily, snapshot_path_valid
from bitpin.flow_context import _read_regular


def with_daily_context(raw, snapshot):
    if not isinstance(raw, dict) or not isinstance(raw.get('context', {}), dict):
        raise ValueError('settings/context must be an object')
    if not snapshot_path_valid(snapshot):
        raise ValueError('expected an absolute path to defi-daily.json')
    result = copy.deepcopy(raw)
    result.setdefault('context', {})['defi_daily_snapshot'] = snapshot
    validate_context_config(result['context'])
    return result


def atomic_settings(path, data):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('settings must be an existing regular file')
    info = path.stat()
    fd, temporary = tempfile.mkstemp(prefix='.defi-settings-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            if hasattr(os, 'fchown'):
                os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            os.chmod(temporary, info.st_mode & 0o777)
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        problems = check_kimi_config(temporary, require_model=True)
        if problems:
            raise ValueError('prospective settings failed validation')
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_restore(source, target):
    """Replace a destination entry atomically; never follow a destination symlink as root."""
    source, target = Path(source), Path(target)
    content = _read_regular(source, 512 * 1024)
    json.loads(content.decode('utf-8-sig'))
    info = source.stat()
    fd, temporary = tempfile.mkstemp(prefix='.defi-restore-', dir=str(target.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            if hasattr(os, 'fchown'):
                os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            os.chmod(temporary, info.st_mode & 0o777)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(target))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def require_stopped():
    state = subprocess.check_output(['systemctl', 'show', '-p', 'ActiveState', '--value',
                                     'bitpin-bot.service']).decode().strip()
    if state not in ('inactive', 'failed'):
        raise ValueError('stop bitpin-bot.service before changing its settings')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kimi-config', default='/etc/bitpin-bot/kimi.json')
    parser.add_argument('--snapshot', default='/var/lib/bitpin-defi-shadow/defi-daily.json')
    parser.add_argument('--apply', action='store_true', help='write the setting; bot must be stopped')
    parser.add_argument('--is-enabled', action='store_true', help='exit 0 if the explicit setting already matches')
    parser.add_argument('--restore-from', help='owner rollback directory created by enable_defi_daily.sh')
    args = parser.parse_args()
    if args.restore_from:
        if not hasattr(os, 'geteuid') or os.geteuid() != 0:
            raise ValueError('rollback requires root')
        require_stopped()
        saved = Path(args.restore_from)
        if saved.is_symlink() or saved.stat().st_uid != 0 or saved.stat().st_mode & 0o077:
            raise ValueError('rollback directory must be private and root-owned')
        atomic_restore(saved / 'kimi.json', Path('/etc/bitpin-bot/kimi.json'))
        atomic_restore(saved / 'LIVE_CONFIRMED', Path('/var/lib/bitpin-bot/LIVE_CONFIRMED'))
        return 0
    # This only reads non-secret JSON settings and public observations.
    config = load_kimi_config(args.kimi_config)
    if args.is_enabled:
        return 0 if config.get('context', {}).get('defi_daily_snapshot') == args.snapshot else 1
    snapshot = load_defi_daily(args.snapshot, time.time())
    required = [m + ':global' for m in ('tvl', 'dex_volume', 'stablecoin_supply', 'fees', 'revenue', 'open_interest')]
    if any(snapshot.get('sources', {}).get(key, {}).get('status') != 'ok' for key in required):
        raise ValueError('activation requires all six fresh global daily indicators')
    print('Public daily context: %s healthy sources; all six global indicators ready.' %
          snapshot['available_sources'])
    if not args.apply:
        print('Read-only preview. No settings changed.')
        return 0
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        raise ValueError('--apply requires root on the server')
    require_stopped()
    path = Path(args.kimi_config)
    with path.open(encoding='utf-8-sig') as stream:
        raw = json.load(stream)
    data = with_daily_context(raw, args.snapshot)
    atomic_settings(path, data)
    print('Set context.defi_daily_snapshot. Owner check/confirm-live/start is required.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print('FAILED: %s' % exc, file=sys.stderr)
        sys.exit(1)
