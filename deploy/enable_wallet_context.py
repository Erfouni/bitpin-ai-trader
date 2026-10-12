#!/usr/bin/env python3
"""Owner-only explicit wallet-context activation, with credential-free preview."""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bitpin.analysis import validate_context_config
from bitpin.brain import load_kimi_config
from bitpin.wallet_context import load_wallet_context, snapshot_path_valid
from deploy.enable_defi_daily import atomic_restore, atomic_settings, require_stopped
from deploy.owner_transaction import owner_transaction

PERSONAL = '/var/lib/bitpin-personal-whale-shadow/personal-whale-shadow.json'
CUSTODY = '/var/lib/bitpin-whale-shadow/whale-shadow.json'


def with_wallet_context(raw, personal, custody):
    if not isinstance(raw, dict) or not isinstance(raw.get('context', {}), dict):
        raise ValueError('settings/context must be an object')
    if not snapshot_path_valid(personal, True) or not snapshot_path_valid(custody, False):
        raise ValueError('expected absolute public personal and custody snapshot paths')
    result = copy.deepcopy(raw)
    result.setdefault('context', {}).update(personal_whale_snapshot=personal, custody_whale_snapshot=custody)
    validate_context_config(result['context'])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kimi-config', default='/etc/bitpin-bot/kimi.json')
    parser.add_argument('--personal-snapshot', default=PERSONAL)
    parser.add_argument('--custody-snapshot', default=CUSTODY)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--is-enabled', action='store_true')
    parser.add_argument('--restore-from')
    args = parser.parse_args()
    if args.apply or args.restore_from:
        with owner_transaction():
            return run(args)
    return run(args)


def run(args):
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
    config = load_kimi_config(args.kimi_config)
    if args.is_enabled:
        return 0 if (config.get('context', {}).get('personal_whale_snapshot') == args.personal_snapshot
                     and config.get('context', {}).get('custody_whale_snapshot') == args.custody_snapshot) else 1
    public = load_wallet_context(args.personal_snapshot, args.custody_snapshot, time.time())
    personal = public.get('personal', {}).get('available_wallets', 0)
    custody = public.get('custody', {}).get('available_wallets', 0)
    if not personal:
        raise ValueError('activation requires at least one fresh two-provider personal ETH observation')
    print('Validated native wallet observations: %d personal ETH, %d historical BTC custody.' % (personal, custody))
    if not custody:
        print('BTC custody is unavailable/disputed and will be excluded until both providers agree.')
    print('Balances and transfers are not trades or calibrated directional signals.')
    if not args.apply:
        print('Read-only preview. No settings, model calls or orders changed.')
        return 0
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        raise ValueError('--apply requires root on the server')
    require_stopped()
    path = Path(args.kimi_config)
    if path.is_symlink() or not path.is_file():
        raise ValueError('settings must be an existing regular file')
    with path.open(encoding='utf-8-sig') as stream:
        raw = json.load(stream)
    atomic_settings(path, with_wallet_context(raw, args.personal_snapshot, args.custody_snapshot))
    print('Set explicit wallet paths. Owner check/confirm-live/start is required.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print('FAILED: %s' % exc, file=sys.stderr)
        sys.exit(1)
