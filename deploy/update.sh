#!/usr/bin/env bash
# Bitpin bot - update the installed code in /opt/bitpin-bot.
#
# Upload and unpack the NEW version into a fresh directory, then run from there:
#     cd ~/bitpin-bot-src-new && sudo bash deploy/update.sh
# Go back to the previous version (read "Rollback" below first):
#     sudo bash /opt/bitpin-bot/deploy/update.sh --rollback
#
# Steps:
#   1. copy the new code to /opt/bitpin-bot.new and check that it compiles with this Python
#   2. run the unit tests (offline, a few seconds) as user bitpin, if tests/ is shipped, and check
#      that the NEW code accepts your /etc/bitpin-bot/config.json and kimi.json (unknown or renamed
#      keys are rejected by the bot). If the live bot is running, also check that the new code
#      accepts its live confirmation (confirm-live --check). If any of this fails the update stops
#      here and the running bot is not touched.
#   3. stop bitpin-bot / bitpin-bot-paper if they are running
#   4. move /opt/bitpin-bot to /opt/bitpin-bot.bak-<UTC time> (the 3 newest backups are kept)
#      and put the new code in place
#   5. install changed unit files (old copies in /var/backups/bitpin-bot), daemon-reload; the v3 ops files
#      (needrestart exclusion, logrotate, the backup timer) idempotently
#   6. start again ONLY the services that were running before. If one of them does not stay up,
#      the previous version is restored and started again automatically (see --no-auto-rollback).
#      The Telegram notifier (bitpin-bot-notify) is stopped and started with them when it was
#      running, but it never causes a rollback: trading does not depend on it.
# The CONTENTS of /etc/bitpin-bot (keys, config.json, kimi.json, notify.env, notify.json) and of
# /var/lib/bitpin-bot* (state, journal, decisions, kill switch) are never modified; the owner and mode of the
# /etc/bitpin-bot files are normalised (bitpin-bot.env / notify.env 0600 root:root, the JSON settings 0640
# root:bitpin; an older version's root:bitpin env file becomes root:root). A kimi.json / notify.env /
# notify.json is created only if it does not exist. New SETTINGS of a version are applied separately and only
# when you run them: sudo python3 deploy/apply_profile.py (backs up, changes only the profile's keys).
# A version that changes what the live bot does on its own (e.g. its resting orders) is refused while
# the live bot runs with the old confirmation: stop it first, update, then confirm-live (DEPLOY_FA.md).
# If the update stops with [FAIL], nothing was changed - but a bot YOU stopped for it stays stopped: start
# it again (sudo systemctl start bitpin-bot). While it is stopped its resting crash-ladder bids stay on
# Bitpin and nothing enforces the code exits of what they buy.
#
# Rollback (--rollback) puts the newest /opt/bitpin-bot.bak-* back. It is REFUSED (nothing changed) when
#   * the previous code rejects the CURRENT /etc/bitpin-bot/config.json / kimi.json - after
#     apply_profile.py they hold settings the older version does not know, and it would exit 78 at every
#     start: restore the *.before-profile copies from /var/backups/bitpin-bot first;
#   * the bot's journal shows resting orders (crash-ladder bids, target sells) on Bitpin: an older version
#     may not manage them (no code exits) and its helper cannot cancel them. First:
#         sudo systemctl stop bitpin-bot && sudo bitpin-bot cancel-resting
# --rollback --force rolls back anyway. After a rollback the old version needs its own confirm-live.
# A restored version without the Telegram notifier gets the notifier's units stopped and disabled.
#
# Options:
#   --skip-tests         do not run the tests (the config check still runs)
#   --no-tests           do not copy tests/ at all
#   --with-data          also copy data/ (local candle CSVs)
#   --no-auto-rollback   keep the new version even if a service does not stay up after the update
#   --rollback           restore the newest /opt/bitpin-bot.bak-* (the current code is moved aside)
#   --force              with --rollback: roll back although the checks above fail
set -euo pipefail
umask 022

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SRC="$(cd "$HERE/.." && pwd -P)"
# shellcheck source=deploy/lib.sh
. "$HERE/lib.sh"

KEEP_BACKUPS=3
IN_ROLLBACK=0        # 1 while a previous version is being restored (--rollback or the automatic one)
NOTIFY_REJECTED=0    # 1 when the code being restored rejects notify.json (its notifier is not started)

usage() {  # the comment block at the top of this file
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"
}

# Every "update aborted" ends here: nothing was changed. A live bot the owner stopped for the update
# (DEPLOY_FA.md) is still stopped, though, and while it is stopped its resting crash-ladder bids are on
# Bitpin with nothing enforcing the code exits of what they buy: say so.
aborted() {  # MESSAGE
    local st
    st="$(systemctl show -p ActiveState --value bitpin-bot.service 2>/dev/null || true)"
    case "$st" in
        active|activating|reloading)
            die "$1 - update aborted: nothing was changed and the running bot was NOT touched." ;;
    esac
    if [ "$(systemctl is-enabled bitpin-bot.service 2>/dev/null || true)" = enabled ] && [ ! -e "$STATE_DIR/STOP" ]; then
        die "$1 - update aborted: nothing was changed. The live bot is NOT running (stopped for this update?): start the old version again with: sudo systemctl start bitpin-bot   (while it is stopped, its resting crash-ladder bids stay on Bitpin and nothing enforces the code exits)"
    fi
    die "$1 - update aborted: nothing was changed."
}

# Run the unit tests of the staged copy DIR (offline, as the bot user). They must pass there although
# stage_code leaves files out of it (tests/test_ops_release.py checks that).
run_tests() {  # DIR
    local dir="$1"
    if [ ! -d "$dir/tests" ]; then
        info "no tests/ in the new version: tests skipped"
        return 0
    fi
    info "running the unit tests (offline) as $APP_USER ..."
    if (cd "$dir" && timeout 900 runuser -u "$APP_USER" -- env HOME="$STATE_DIR" TZ=UTC \
            PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -m unittest discover -s tests -q); then
        ok "tests passed"
        return 0
    fi
    return 1
}

# Check the existing /etc/bitpin-bot config files with the NEW code (as the bot user, offline) before
# anything is stopped: a renamed or removed key would otherwise stop the bot at every start. A rollback
# checks them with the code it is about to restore. NOTIFY_STRICT 0: a notify.json that code rejects is
# only a warning (NOTIFY_REJECTED=1: its notifier is then not started) - trading does not depend on it.
check_configs() {  # DIR [NOTIFY_STRICT(1/0)]
    local dir="$1" notify_strict="${2:-1}"
    local -a files=()
    [ -f "$ETC_DIR/config.json" ] && files+=(--config "$ETC_DIR/config.json")
    [ -f "$ETC_DIR/kimi.json" ] && files+=(--kimi-config "$ETC_DIR/kimi.json")
    if [ "${#files[@]}" -eq 0 ]; then
        info "no config files in $ETC_DIR yet: nothing to check"
        return 0
    fi
    if [ ! -f "$dir/deploy/check_server.py" ]; then
        warn "the code in $dir has no deploy/check_server.py: config files NOT checked"
        return 0
    fi
    info "checking $ETC_DIR/config.json and kimi.json with the code in $dir ..."
    if ! (cd "$dir" && timeout 300 runuser -u "$APP_USER" -- env HOME="$STATE_DIR" TZ=UTC \
            PYTHONDONTWRITEBYTECODE=1 "$PYTHON" "$dir/deploy/check_server.py" --config-only "${files[@]}"); then
        return 1
    fi
    ok "the code in $dir accepts the existing config files"
    # the Telegram notifier's settings (no secrets in it): a notify.json the new code rejects would stop the
    # notifier at every start - refused like a bad config.json (compare it with notify.example.json)
    if [ -f "$NOTIFY_CONFIG" ] && [ -f "$dir/bitpin/notify.py" ]; then
        if ! (cd "$dir" && timeout 60 runuser -u "$APP_USER" -- env HOME="$STATE_DIR" TZ=UTC \
                PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -c 'import sys; sys.path.insert(0, sys.argv[1]); from bitpin import notify; notify.load_config(sys.argv[2])' \
                "$dir" "$NOTIFY_CONFIG"); then
            if [ "$notify_strict" = 0 ]; then
                warn "the code in $dir rejects $NOTIFY_CONFIG: its Telegram notifier will not be started (trading is not affected)"
                NOTIFY_REJECTED=1
                return 0
            fi
            warn "the new version rejects $NOTIFY_CONFIG (compare it with notify.example.json)"
            return 1
        fi
        ok "the code in $dir accepts $NOTIFY_CONFIG"
    fi
    return 0
}

# The Telegram notifier runs code from $APP_DIR too: stopped with the trading units, started again
# afterwards when it was running. It never triggers a rollback of the trading bot.
NOTIFY_WAS_RUNNING=0
stop_notifier() {
    if [ "$(systemctl show -p ActiveState --value bitpin-bot-notify.service 2>/dev/null)" = active ]; then
        NOTIFY_WAS_RUNNING=1
        info "stopping bitpin-bot-notify (Telegram notifier) for the code swap"
        systemctl stop bitpin-bot-notify.service || true
    fi
}
restart_notifier() {
    [ "$NOTIFY_WAS_RUNNING" = 1 ] || return 0
    NOTIFY_WAS_RUNNING=0
    if systemctl start bitpin-bot-notify.service; then
        ok "bitpin-bot-notify (Telegram notifier) started again"
    else
        warn "the Telegram notifier did not start (trading is not affected): sudo journalctl -u bitpin-bot-notify -n 50"
    fi
}

# The running live service must be allowed to start again with the NEW code: its LIVE_CONFIRMED
# (written by confirm-live) must match the settings as the new code reads them. Checked read-only,
# as the bot user, before anything is stopped.
check_live_confirmation() {  # DIR
    local dir="$1"
    info "checking that the new code accepts the live confirmation ($STATE_DIR/LIVE_CONFIRMED) ..."
    if (cd "$dir" && timeout 300 runuser -u "$APP_USER" -- env HOME="$STATE_DIR" TZ=UTC \
            PYTHONDONTWRITEBYTECODE=1 "$PYTHON" "$dir/scripts/run_bot.py" confirm-live --check \
            --config "$ETC_DIR/config.json" --kimi-config "$ETC_DIR/kimi.json" --state-dir "$STATE_DIR"); then
        ok "the live confirmation is still valid with the new code"
        return 0
    fi
    return 1
}

stop_units() {  # UNITS...
    local u
    for u in "$@"; do
        info "stopping $u"
        systemctl stop "$u"
    done
}

start_units() {  # UNITS...
    local u bad=0
    [ $# -gt 0 ] || return 0
    for u in "$@"; do
        info "starting $u (it was running before the update)"
        systemctl start "$u" || true
    done
    sleep 15
    for u in "$@"; do
        if systemctl is-active --quiet "$u"; then
            ok "$u is running"
        else
            bad=1
            warn "$u did not stay up. Last log lines:"
            journalctl -u "$u" -n 30 --no-pager >&2 || true
        fi
    done
    if [ "$bad" = 1 ]; then
        if [ "$IN_ROLLBACK" = 1 ]; then
            # --rollback again would restore an even OLDER backup, not fix this one
            warn "the RESTORED version did not stay up either. Do NOT run --rollback again (it would restore an even older backup). Check: sudo journalctl -u bitpin-bot -n 50 ; sudo bitpin-bot health ; an older version needs its own confirm-live and settings it accepts (docs/DEPLOY_FA.md)"
        else
            warn "to go back to the previous version (read docs/DEPLOY_FA.md first): sudo bash $APP_DIR/deploy/update.sh --rollback"
        fi
        return 1
    fi
    return 0
}

# After the code swap of a rollback: a restored tree without the Telegram notifier (or one that rejects
# notify.json) must not leave the notifier's units enabled - they would point at a missing script and
# restart forever. Stopped and disabled; the next update installs and the owner enables them again.
settle_notifier() {
    local u
    if [ -f "$APP_DIR/scripts/notify_bot.py" ] && [ "$NOTIFY_REJECTED" = 0 ]; then
        return 0
    fi
    NOTIFY_WAS_RUNNING=0
    for u in bitpin-bot-notify.service bitpin-bot-notify-stop.path bitpin-bot-notify-stop.service; do
        [ -f "$UNIT_DIR/$u" ] || continue
        systemctl disable --now "$u" >/dev/null 2>&1 || true
    done
    warn "the restored version has no usable Telegram notifier: its units were stopped and disabled (trading is not affected; enable it again after the next update: docs/TELEGRAM_FA.md)"
}

prune_backups() {
    local -a all
    local i n
    mapfile -t all < <(ls -1d "$APP_DIR".bak-* 2>/dev/null | sort)
    n=${#all[@]}
    if [ "$n" -gt "$KEEP_BACKUPS" ]; then
        for ((i = 0; i < n - KEEP_BACKUPS; i++)); do
            rm -rf "${all[$i]:?}"
            info "removed old code backup ${all[$i]}"
        done
    fi
    # restore_backup parks the rejected code in $APP_DIR.failed-<stamp>. Nothing else ever removes
    # those, so a few failed updates would leave an unbounded number of full copies in /opt (~40 MB
    # each with --with-data) on a small VPS that runs other services too. Keep only the newest.
    mapfile -t all < <(ls -1d "$APP_DIR".failed-* 2>/dev/null | sort)
    n=${#all[@]}
    if [ "$n" -gt 1 ]; then
        for ((i = 0; i < n - 1; i++)); do
            rm -rf "${all[$i]:?}"
            info "removed old failed-update copy ${all[$i]}"
        done
    fi
}

# After a rollback to a version without the v3 ops files (e.g. v2): stop and remove the ops units and
# system files the restored tree does not have - the backup timer would otherwise fail every day (its script
# is gone) and the old uninstall.sh would never remove them.
drop_foreign_ops() {  # DEPLOY_DIR
    local u name dst removed=0
    for u in $OPS_UNITS $PANEL_UNITS; do
        if [ ! -f "$1/$u" ] && [ -f "$UNIT_DIR/$u" ]; then
            systemctl disable --now "$u" >/dev/null 2>&1 || true
            rm -f "$UNIT_DIR/$u"
            removed=1
            warn "removed $u: the restored version does not have it"
        fi
    done
    [ "$removed" = 1 ] && { systemctl daemon-reload >/dev/null 2>&1 || true; }
    while read -r name dst; do
        [ -n "$name" ] || continue
        if [ ! -f "$1/$name" ] && [ -f "$dst" ]; then
            rm -f "$dst"
            warn "removed $dst: the restored version does not have it"
        fi
    done <<< "$(ops_conf_paths)"
    return 0
}

# v3.1: a RUNNING management panel serves the new code only after a restart (the root helper starts per
# request, so it always runs the code on disk). Never started here when it is not running.
restart_panel() {
    [ -f "$UNIT_DIR/bitpin-bot-panel.service" ] || return 0
    if systemctl is-active --quiet bitpin-bot-panel.service 2>/dev/null; then
        if systemctl try-restart bitpin-bot-panel.service >/dev/null 2>&1; then
            ok "management panel restarted with the new code"
        else
            warn "could not restart bitpin-bot-panel (sudo systemctl restart bitpin-bot-panel)"
        fi
    fi
    return 0
}

# Put BACKUP back as $APP_DIR (the current code goes to $APP_DIR.failed-<time>) and start UNITS again.
restore_backup() {  # BACKUP UNITS...
    local backup="$1" u
    shift
    for u in "$@"; do
        systemctl stop "$u" 2>/dev/null || true
    done
    mv "$APP_DIR" "$APP_DIR.failed-$STAMP"
    mv "$backup" "$APP_DIR"
    ok "restored $backup; the replaced code is in $APP_DIR.failed-$STAMP"
    install_units "$APP_DIR/deploy"
    install_ops_files "$APP_DIR/deploy"
    drop_foreign_ops "$APP_DIR/deploy"
    install_cli_link
    check_cli_flags || true
    settle_notifier
    restart_panel
    for u in "$@"; do
        systemctl reset-failed "$u" 2>/dev/null || true
    done
    start_units "$@"
}

rollback() {  # FORCE(0/1)
    local force="${1:-0}" last running n
    IN_ROLLBACK=1
    last="$(ls -1d "$APP_DIR".bak-* 2>/dev/null | sort | tail -n 1 || true)"
    [ -n "$last" ] || die "no backup found ($APP_DIR.bak-*)"
    info "rolling back to $last"
    # 1. The code being restored must accept the CURRENT settings files. After apply_profile.py they hold
    #    settings of the newer version (e.g. config.json "ladder", kimi.json llm "stream"): the old code
    #    would exit 78 at every start and never trade again.
    if ! check_configs "$last" 0; then
        if [ "$force" = 1 ]; then
            warn "--force: rolling back although $last rejects $ETC_DIR/config.json / kimi.json - that version will NOT start until they match it"
        else
            die "the previous version ($last) rejects your current $ETC_DIR/config.json / kimi.json (they hold settings of the newer version) and would never start. Nothing was changed. Restore the settings it used first - the *.before-profile copies in $BACKUP_DIR (docs/DEPLOY_FA.md, rollback) - then run --rollback again (--rollback --force: roll back anyway)."
        fi
    fi
    # 2. Resting orders of the current version (crash-ladder bids, target sells): an older version may not
    #    manage them - no code exits for what the bids buy - and its helper has no cancel-resting.
    n="$(resting_count "$STATE_DIR")"
    if [ "$n" != 0 ]; then
        if [ "$force" = 1 ]; then
            warn "--force: rolling back with $n resting bot order(s) on Bitpin (journal $STATE_DIR/live_orders.json) that the restored version may not manage - check them in the Bitpin app"
        else
            die "the live bot's journal shows $n resting order(s) on Bitpin (crash-ladder bids / target sells; '?' = the journal cannot be read). The previous version may not manage them (no code exits for what the bids buy) and its helper cannot cancel them. Nothing was changed. Cancel them FIRST, while this version's helper is installed:  sudo systemctl stop bitpin-bot && sudo bitpin-bot cancel-resting   then run --rollback again (--rollback --force: roll back anyway)."
        fi
    fi
    running="$(running_units)"
    stop_notifier
    # shellcheck disable=SC2086
    if ! restore_backup "$last" $running; then
        restart_notifier
        exit 1
    fi
    restart_notifier
    if [ -z "$running" ]; then
        info "no bot service was running: the restored version is NOT started. It needs its own live confirmation: sudo bitpin-bot check && sudo bitpin-bot confirm-live && sudo systemctl start bitpin-bot"
    fi
    exit 0
}

# After a failed start of the new version: restore the version that was running before.
auto_rollback() {  # BACKUP AUTO(0/1) UNITS...
    local backup="$1" auto="$2"
    shift 2
    if [ "$auto" != 1 ]; then
        warn "keeping the new version (--no-auto-rollback). Go back with (read docs/DEPLOY_FA.md first): sudo bash $APP_DIR/deploy/update.sh --rollback"
        restart_notifier
        exit 1
    fi
    IN_ROLLBACK=1        # the version being restored ran a moment ago with these very settings files
    warn "the new version did not start properly: restoring the previous version automatically"
    if restore_backup "$backup" "$@"; then
        warn "update ROLLED BACK: the previous version runs again. The new code is in $APP_DIR.failed-$STAMP"
    else
        warn "the previous version did not stay up either - check: sudo bitpin-bot health"
    fi
    restart_notifier
    exit 1
}

main() {
    local skip_tests=0 with_tests=1 with_data=0 do_rollback=0 auto=1 force=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --skip-tests) skip_tests=1 ;;
            --no-tests) with_tests=0 ;;
            --with-data) with_data=1 ;;
            --no-auto-rollback) auto=0 ;;
            --rollback) do_rollback=1 ;;
            --force) force=1 ;;
            -h|--help) usage; exit 0 ;;
            *) usage; die "unknown option: $1" ;;
        esac
        shift
    done

    require_root
    check_systemd
    [ -d "$APP_DIR" ] || die "$APP_DIR not found: install first with: sudo bash deploy/install.sh"
    getent passwd "$APP_USER" >/dev/null || die "user $APP_USER not found: install first with: sudo bash deploy/install.sh"

    if [ "$do_rollback" = 1 ]; then
        rollback "$force"
    elif [ "$force" = 1 ]; then
        die "--force is only used with --rollback"
    fi

    [ "$SRC" != "$APP_DIR" ] || die "run update.sh from the NEW unpacked project directory, not from $APP_DIR"
    check_source_tree "$SRC"
    check_python
    info "updating $APP_DIR from $SRC"

    # 1-2: prepare and test while the old version keeps running
    local stage="$APP_DIR.new"
    stage_code "$SRC" "$stage" "$with_tests" "$with_data"
    if ! syntax_check "$stage"; then
        rm -rf "${stage:?}"
        aborted "the new code does not compile with this server's Python"
    fi
    normalize_tree "$stage"
    if [ "$skip_tests" = 0 ] && [ "$with_tests" = 1 ]; then
        if ! run_tests "$stage"; then
            rm -rf "${stage:?}"
            aborted "tests failed (use --skip-tests to override)"
        fi
    else
        info "tests skipped"
    fi
    if ! check_configs "$stage"; then
        rm -rf "${stage:?}"
        aborted "the new version rejects your config file(s) above. Compare them with the new examples (config.example.json, kimi.example.json) and fix them first"
    fi

    # 3: stop what is running (remember it)
    local running
    running="$(running_units)"
    case " $running " in
        *" bitpin-bot.service "*)
            if ! check_live_confirmation "$stage"; then
                rm -rf "${stage:?}"
                die "the new version would not start the live bot with the current confirmation (a new version that places resting orders on its own needs a new confirm-live) - update aborted; the running bot was NOT touched. To update: sudo systemctl stop bitpin-bot, run update.sh again, then (new settings: sudo python3 deploy/apply_profile.py) sudo bitpin-bot check && sudo bitpin-bot confirm-live && sudo systemctl start bitpin-bot   (docs/DEPLOY_FA.md)"
            fi ;;
    esac
    if [ -n "$running" ]; then
        # shellcheck disable=SC2086
        stop_units $running
    else
        info "no bot service is running"
    fi
    stop_notifier

    # 4: backup + swap
    local backup="$APP_DIR.bak-$STAMP"
    mv "$APP_DIR" "$backup"
    mv "$stage" "$APP_DIR"
    compile_tree "$APP_DIR"
    ok "code updated; previous version saved as $backup"
    prune_backups

    # 5: units, helper, config files that are still missing (existing ones are kept)
    ensure_dirs
    install_config_files "$APP_DIR"
    install_units "$APP_DIR/deploy"
    install_ops_files "$APP_DIR/deploy"
    enable_backup_timer
    install_cli_link
    restart_panel
    local flags_ok=1
    check_cli_flags || flags_ok=0
    if [ -f "$ETC_DIR/config.json" ] && ! cmp -s "$backup/config.example.json" "$APP_DIR/config.example.json" 2>/dev/null; then
        info "config.example.json changed: compare it with $ETC_DIR/config.json for new settings:"
        info "    diff $ETC_DIR/config.json $APP_DIR/config.example.json"
    fi
    if [ -f "$ETC_DIR/kimi.json" ] && ! cmp -s "$backup/kimi.example.json" "$APP_DIR/kimi.example.json" 2>/dev/null; then
        info "kimi.example.json changed ($ETC_DIR/kimi.json is never changed by update.sh): compare them:"
        info "    diff $ETC_DIR/kimi.json $APP_DIR/kimi.example.json"
        info "then: sudo bitpin-bot check ; sudo bitpin-bot kimi-check --news ; and after any edit: sudo bitpin-bot confirm-live"
    fi
    if [ -f "$APP_DIR/deploy/apply_profile.py" ]; then
        info "to apply this version's recommended settings to kimi.json / config.json (backup first, only the"
        info "profile's keys; see docs/DEPLOY_FA.md):  sudo python3 $SRC/deploy/apply_profile.py --dry-run"
    fi
    if [ -f "$NOTIFY_CONFIG" ] && [ -f "$backup/notify.example.json" ] \
            && ! cmp -s "$backup/notify.example.json" "$APP_DIR/notify.example.json" 2>/dev/null; then
        info "notify.example.json changed ($NOTIFY_CONFIG is never changed by update.sh): compare them:"
        info "    diff $NOTIFY_CONFIG $APP_DIR/notify.example.json"
    fi

    # 6: the new code must be startable AT ALL - checked before the "nothing was running" exit, or a
    # version whose run_bot.py lost a flag the units pass would stay installed with only a WARN line
    # and update.sh would exit 0. The services would then fail with argparse status 2, which is
    # neither 0 nor 78, so Restart=always retries them forever.
    if [ "$flags_ok" = 0 ]; then
        warn "the new run_bot.py does not support the flags the service units pass: it cannot start."
        # shellcheck disable=SC2086
        auto_rollback "$backup" "$auto" $running
    fi
    # 6b: restart only what was running
    if [ -z "$running" ]; then
        info "services were not running before the update: not starting them"
        restart_notifier
        exit 0
    fi
    # shellcheck disable=SC2086
    if ! start_units $running; then
        # shellcheck disable=SC2086
        auto_rollback "$backup" "$auto" $running
    fi
    restart_notifier
    ok "update finished"
}

main "$@"
