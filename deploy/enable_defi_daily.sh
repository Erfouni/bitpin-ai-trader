#!/usr/bin/env bash
# OWNER ONLY: add daily macro context, check, type confirm-live, then restart.
# Cancellation/failure restores the old settings and live-confirmation record.
set -euo pipefail
umask 077
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
. "$HERE/lib.sh"
require_root
check_systemd
[ -t 0 ] || die "run this interactively; the owner must type confirm-live"
[ "$HERE" = "$APP_DIR/deploy" ] || die "install the new code before activation"
[ -f "$ETC_DIR/kimi.json" ] && [ ! -L "$ETC_DIR/kimi.json" ] || die "missing regular kimi.json"
[ -f "$STATE_DIR/LIVE_CONFIRMED" ] && [ ! -L "$STATE_DIR/LIVE_CONFIRMED" ] || die "missing live confirmation"

# Read-only, credential-free preflight as the service user.
runuser -u "$APP_USER" -- env PYTHONDONTWRITEBYTECODE=1 "$PYTHON" "$HERE/enable_defi_daily.py"
runuser -u "$APP_USER" -- env PYTHONDONTWRITEBYTECODE=1 "$PYTHON" "$APP_DIR/scripts/run_bot.py" \
    confirm-live --check --config "$ETC_DIR/config.json" --kimi-config "$ETC_DIR/kimi.json" --state-dir "$STATE_DIR"
if runuser -u "$APP_USER" -- env PYTHONDONTWRITEBYTECODE=1 "$PYTHON" "$HERE/enable_defi_daily.py" --is-enabled; then
    ok "daily context is already enabled and confirmed; nothing changed"
    exit 0
fi
WAS_RUNNING=0
case "$(systemctl show -p ActiveState --value bitpin-bot.service)" in
    active|activating|reloading|deactivating) WAS_RUNNING=1 ;;
esac
SAVE="$BACKUP_DIR/defi-daily-$STAMP-$$"
mkdir -p "$SAVE"
chmod 0700 "$SAVE"
cp -a "$ETC_DIR/kimi.json" "$SAVE/kimi.json"
cp -a "$STATE_DIR/LIVE_CONFIRMED" "$SAVE/LIVE_CONFIRMED"
CHANGED=0
FINISHED=0
restore_on_failure() {
    local rc=$?
    trap - EXIT INT TERM
    if [ "$FINISHED" != 1 ]; then
        if [ "$CHANGED" = 1 ]; then
            systemctl stop bitpin-bot.service || true
            if ! "$PYTHON" "$HERE/enable_defi_daily.py" --restore-from "$SAVE"; then
                warn "rollback failed; bot remains stopped; recover settings from $SAVE"
                exit 1
            fi
            if ! runuser -u "$APP_USER" -- env PYTHONDONTWRITEBYTECODE=1 "$PYTHON" "$APP_DIR/scripts/run_bot.py" \
                confirm-live --check --config "$ETC_DIR/config.json" --kimi-config "$ETC_DIR/kimi.json" --state-dir "$STATE_DIR"; then
                warn "restored settings could not be confirmed; bot remains stopped; backup: $SAVE"
                exit 1
            fi
        fi
        if [ "$WAS_RUNNING" = 1 ]; then
            systemctl start bitpin-bot.service || warn "could not restore the previously running bot"
        fi
        warn "activation did not finish; previous settings restored; backup: $SAVE"
        [ "$rc" -ne 0 ] || rc=1
    fi
    exit "$rc"
}
trap restore_on_failure EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
systemctl stop bitpin-bot.service
CHANGED=1
"$PYTHON" "$HERE/enable_defi_daily.py" --apply
"$CLI_LINK" check
"$CLI_LINK" confirm-live
systemctl start bitpin-bot.service
sleep 2
systemctl is-active --quiet bitpin-bot.service
FINISHED=1
ok "daily context enabled; bot active; backup: $SAVE"
