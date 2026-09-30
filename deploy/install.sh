#!/usr/bin/env bash
# Bitpin bot - Ubuntu installer. Safe to run more than once.
#
# Run it with sudo from the unpacked project directory:
#     cd ~/bitpin-bot-src && sudo bash deploy/install.sh
#
# What it does:
#   * checks Ubuntu, systemd, /usr/bin/python3 >= 3.8, CA certificates and time synchronisation
#   * creates the system user "bitpin" (no login shell)
#   * copies the code to /opt/bitpin-bot (root-owned, read-only for the bot; without data/, state/,
#     scratch/, caches or anything that looks like a key file). The previous copy, if any, is kept
#     as /opt/bitpin-bot.prev
#   * creates /var/lib/bitpin-bot and /var/lib/bitpin-bot-paper (0700 bitpin) and /etc/bitpin-bot
#     (0750 root:bitpin)
#   * creates /etc/bitpin-bot/bitpin-bot.env, config.json and kimi.json (and the Telegram notifier's
#     notify.env and notify.json) from the examples ONLY if they do not exist yet - your keys and
#     settings are never overwritten. Owner and mode (set again on existing files): the secrets files
#     bitpin-bot.env and notify.env 0600 root:root (read by systemd only, never by the bitpin user -
#     do not "fix" them to 0640 root:bitpin), the JSON settings 0640 root:bitpin
#   * creates /var/lib/bitpin-bot-notify (0700 bitpin), the Telegram notifier's own state dir
#   * installs bitpin-bot.service, bitpin-bot-paper.service and the (optional) Telegram notifier units
#     bitpin-bot-notify.service, bitpin-bot-notify-stop.path/.service, and runs systemctl daemon-reload
#   * v3 ops (year_review/Y2): the failure reporter bitpin-bot-failed@.service (exit 78 -> Telegram alert
#     through the notifier), the daily state backup bitpin-bot-backup.service/.timer (03:10 UTC,
#     /var/backups/bitpin-bot/state-<date>.tgz, 14 kept), /etc/needrestart/conf.d/bitpin-bot.conf (the
#     two trading units are never restarted by apt's needrestart) and /etc/logrotate.d/bitpin-bot
#     (bot_live.log 20 MB x 5, copytruncate); nothing system-wide (the journal settings are untouched)
#   * installs the helper command /usr/local/bin/bitpin-bot
# It does NOT enable or start any bot service; the only unit it enables is the daily backup timer
# (root only, no trading, no network). It prints the next steps instead.
#
# Options:
#   --no-tests     do not copy tests/ (update.sh then cannot run them)
#   --with-data    also copy data/ (local candle CSVs: optional history seed, ~40 MB)
#   --enable-ntp   run "timedatectl set-ntp true" if no time sync service is active (a system-wide
#                  setting: only on a server you administer; never needed on a synchronised server)
#   --force        continue on a non-Ubuntu system
set -euo pipefail
umask 022

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SRC="$(cd "$HERE/.." && pwd -P)"
# shellcheck source=deploy/lib.sh
. "$HERE/lib.sh"

usage() {  # the comment block at the top of this file
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"
}

print_next_steps() {
    cat <<EOF

=====================================================================================
 Installed. Nothing is running yet. Next steps (Persian guide: $APP_DIR/docs/DEPLOY_FA.md):

 1. Put your keys in the env file (BITPIN_API_KEY, BITPIN_SECRET_KEY, KIMI_API_KEY) and check the
    line KIMI_HTTPS_PROXY=http://127.0.0.1:1081 (the local HTTP proxy Kimi is reached through;
    only Kimi traffic uses it, Bitpin always goes direct). Then check the server and both config
    files (ready-made examples) - it must end with RESULT: OK:
        sudo nano $ENV_FILE
        sudo bitpin-bot check
    Whitelist the server's outbound IP (the check prints it) for the Bitpin API key; if the check
    shows more than one outbound IP, add each of them (test: sudo bitpin-bot check --auth).
 2. Confirm the toman (IRT) balance the bot sees (it must match your Bitpin account):
        sudo bitpin-bot status
 3. Kimi: key, route, both models (kimi-k3 decides, kimi-k2.6 researches the news) and one real
    news research call - it must end with NEWS: OK and RESULT: OK:
        sudo bitpin-bot kimi-check --news
 4. Optional dry run with paper money (PAPER_CAPITAL_IRT toman, real Kimi calls, no real orders):
        sudo systemctl start bitpin-bot-paper     (logs: sudo journalctl -u bitpin-bot-paper -f)
        sudo systemctl stop bitpin-bot-paper      (stop it before going live)
 5. One-time live confirmation (interactive), then start the live service:
        sudo bitpin-bot confirm-live
        sudo systemctl enable --now bitpin-bot
 6. Watch it:   sudo journalctl -u bitpin-bot -f      Daily:  sudo bitpin-bot health
    State backups (daily 03:10 UTC, 14 kept):  sudo ls -la $BACKUP_DIR/   or now:  sudo bitpin-bot backup
    The crash ladder the bot keeps on Bitpin:  sudo bitpin-bot ladder
    Emergency:  sudo systemctl stop bitpin-bot     (or the kill switch: sudo bitpin-bot stop)
    The bot's resting orders (ladder bids, target sells) stay on Bitpin while it is stopped; to cancel
    them too:  sudo systemctl stop bitpin-bot && sudo bitpin-bot cancel-resting
 7. Optional Telegram notifier (Persian guide: $APP_DIR/docs/TELEGRAM_FA.md):
        sudo nano $NOTIFY_ENV_FILE           (TELEGRAM_BOT_TOKEN; send /start to your bot)
        sudo bitpin-bot notify-setup            (shows your chat id -> TELEGRAM_CHAT_ID)
        sudo bitpin-bot notify-test
        sudo systemctl enable --now bitpin-bot-notify
        sudo systemctl enable --now bitpin-bot-notify-stop.path   (only if you want /stop from Telegram)
 Only ONE bot may use the Bitpin API key: stop any other copy (e.g. on your PC).
=====================================================================================
EOF
}

main() {
    local with_tests=1 with_data=0 force=0 enable_ntp=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --no-tests) with_tests=0 ;;
            --with-data) with_data=1 ;;
            --force) force=1 ;;
            --enable-ntp) enable_ntp=1 ;;
            -h|--help) usage; exit 0 ;;
            *) usage; die "unknown option: $1" ;;
        esac
        shift
    done

    require_root
    check_source_tree "$SRC"
    info "installing from $SRC"
    check_os "$force"
    check_systemd
    check_python

    ensure_user

    if [ "$SRC" = "$APP_DIR" ]; then
        info "running from $APP_DIR itself: code copy skipped"
    else
        local running
        running="$(running_units)"
        if [ -n "$running" ]; then
            die "running now: $running. To replace the code of a running bot use: sudo bash deploy/update.sh"
        fi
        local stage="$APP_DIR.new"
        info "copying the code to $stage"
        stage_code "$SRC" "$stage" "$with_tests" "$with_data"
        info "checking that every .py file compiles with $("$PYTHON" -V 2>&1)"
        if ! syntax_check "$stage"; then
            rm -rf "${stage:?}"
            die "the code does not compile with this server's Python - nothing was installed"
        fi
        normalize_tree "$stage"
        if [ -d "$APP_DIR" ]; then
            rm -rf "${APP_DIR:?}.prev"
            mv "$APP_DIR" "$APP_DIR.prev"
            info "previous code kept as $APP_DIR.prev"
        fi
        mv "$stage" "$APP_DIR"
        compile_tree "$APP_DIR"
        ok "code installed in $APP_DIR (root-owned; the bot cannot modify it)"
    fi

    ensure_dirs
    install_config_files "$APP_DIR"
    install_units "$APP_DIR/deploy"
    install_ops_files "$APP_DIR/deploy"
    enable_backup_timer
    install_cli_link

    check_cli_flags || true
    check_time "$enable_ntp"

    local u
    for u in $UNITS $NOTIFY_UNITS; do
        if systemctl is-enabled --quiet "$u" 2>/dev/null; then
            info "$u is enabled (it was enabled before; install.sh never enables anything)"
        fi
    done
    print_next_steps
}

main "$@"
