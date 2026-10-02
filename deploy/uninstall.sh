#!/usr/bin/env bash
# Bitpin bot - uninstall.
#
#     sudo bash /opt/bitpin-bot/deploy/uninstall.sh            remove the services and the code
#     sudo bash /opt/bitpin-bot/deploy/uninstall.sh --purge    ... and ALSO keys, settings, state, user
#     ... --force    uninstall although the live order journal shows resting bot orders on Bitpin
#
# Without --purge it stops and disables bitpin-bot / bitpin-bot-paper and the Telegram notifier
# (bitpin-bot-notify, bitpin-bot-notify-stop.path/.service) and the v3 ops units (bitpin-bot-failed@,
# bitpin-bot-backup.service/.timer), removes the unit files, the v3 system files (the needrestart
# exclusion, the logrotate rule), the helper /usr/local/bin/bitpin-bot and the code
# (/opt/bitpin-bot and its backups), and KEEPS:
#   /etc/bitpin-bot       (bitpin-bot.env with your API keys, notify.env with the Telegram token,
#                          config.json, kimi.json, notify.json)
#   /var/lib/bitpin-bot*  (bot state, order journal, Kimi decisions log, logs; the notifier's state)
#   the system user "bitpin"
# --purge also deletes those (asks you to type PURGE first; --yes skips the question).
#
# Nothing here touches your Bitpin account: open positions and open orders stay as they are - also the
# bot's RESTING limit orders (crash-ladder bids, target sells). Cancel those BEFORE uninstalling, while
# the helper still exists:  sudo systemctl stop bitpin-bot && sudo bitpin-bot cancel-resting
# (or cancel them in the Bitpin app). While the live order journal shows such orders, uninstall REFUSES
# (nothing is removed) unless --force is given: after it, a ladder bid can still fill with no code exits
# guarding the coin, and cancel-resting no longer exists. If you stop using the bot for good, also delete the API keys in
# the Bitpin and Moonshot panels and the Telegram bot in @BotFather.
set -euo pipefail
umask 022

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=deploy/lib.sh
. "$HERE/lib.sh"

usage() {  # the comment block at the top of this file
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"
}

main() {
    local purge=0 yes=0 force=0 u d n
    while [ $# -gt 0 ]; do
        case "$1" in
            --purge) purge=1 ;;
            --yes|-y) yes=1 ;;
            --force) force=1 ;;
            -h|--help) usage; exit 0 ;;
            *) usage; die "unknown option: $1" ;;
        esac
        shift
    done
    require_root

    # the bot's own resting orders would stay on Bitpin with nothing that guards what they buy, and the
    # helper that cancels them is removed below: refuse first (nothing is removed)
    n="$(resting_count "$STATE_DIR")"
    if [ "$n" != 0 ]; then
        if [ "$force" = 1 ]; then
            warn "--force: uninstalling with $n resting bot order(s) on Bitpin (journal $STATE_DIR/live_orders.json) - they stay there: cancel them in the Bitpin app"
        else
            die "the live bot's journal shows $n resting order(s) on Bitpin (crash-ladder bids / target sells; '?' = the journal cannot be read). After uninstall they stay on Bitpin - a bid can still fill with no code exits guarding the coin - and the helper that cancels them is gone. Nothing was removed. Cancel them FIRST:  sudo systemctl stop bitpin-bot && sudo bitpin-bot cancel-resting   then run uninstall again (--force: uninstall anyway)."
        fi
    fi

    if [ "$purge" = 1 ] && [ "$yes" = 0 ]; then
        echo "--purge deletes PERMANENTLY: $ETC_DIR (API keys, Telegram token, settings), $STATE_DIR,"
        echo "$PAPER_STATE_DIR and $NOTIFY_STATE_DIR (state, order journal, Kimi decisions, logs) and the user $APP_USER."
        echo "Copy anything you want to keep first, e.g.: sudo cp $STATE_DIR/kimi_decisions.jsonl ~/"
        local answer=""
        read -r -p "Type PURGE to continue: " answer || true
        [ "$answer" = "PURGE" ] || die "not confirmed - nothing was removed"
    fi

    for u in $UNITS $NOTIFY_UNITS; do
        if [ -f "$UNIT_DIR/$u" ]; then
            systemctl disable --now "$u" 2>/dev/null || systemctl stop "$u" 2>/dev/null || true
            rm -f "$UNIT_DIR/$u"
            ok "stopped, disabled and removed $u"
        fi
        if [ "$purge" = 1 ] && [ -d "$UNIT_DIR/$u.d" ]; then
            rm -rf "${UNIT_DIR:?}/$u.d"
            ok "removed drop-in directory $UNIT_DIR/$u.d"
        fi
    done
    # the v3 ops units (the failure reporter is a template: its instances are reset too) and system files
    for u in $OPS_UNITS; do
        if [ -f "$UNIT_DIR/$u" ]; then
            systemctl disable --now "$u" 2>/dev/null || systemctl stop "$u" 2>/dev/null || true
            rm -f "$UNIT_DIR/$u"
            ok "stopped, disabled and removed $u"
        fi
        if [ "$purge" = 1 ] && [ -d "$UNIT_DIR/$u.d" ]; then
            rm -rf "${UNIT_DIR:?}/$u.d"
            ok "removed drop-in directory $UNIT_DIR/$u.d"
        fi
    done
    # the v3.1 management panel's units
    for u in $PANEL_UNITS; do
        if [ -f "$UNIT_DIR/$u" ]; then
            systemctl disable --now "$u" 2>/dev/null || systemctl stop "$u" 2>/dev/null || true
            rm -f "$UNIT_DIR/$u"
            ok "stopped, disabled and removed $u"
        fi
        if [ "$purge" = 1 ] && [ -d "$UNIT_DIR/$u.d" ]; then
            rm -rf "${UNIT_DIR:?}/$u.d"
            ok "removed drop-in directory $UNIT_DIR/$u.d"
        fi
    done
    remove_ops_files
    systemctl daemon-reload
    for u in $UNITS $NOTIFY_UNITS; do systemctl reset-failed "$u" 2>/dev/null || true; done
    for u in $OPS_UNITS bitpin-bot-failed@bitpin-bot.service bitpin-bot-failed@bitpin-bot-paper.service; do
        systemctl reset-failed "$u" 2>/dev/null || true
    done

    if [ -L "$CLI_LINK" ]; then
        rm -f "$CLI_LINK"
        ok "removed $CLI_LINK"
    fi

    for d in "$APP_DIR" "$APP_DIR.new" "$APP_DIR.prev" "$APP_DIR".bak-* "$APP_DIR".failed-*; do
        if [ -d "$d" ]; then
            rm -rf "${d:?}"
            ok "removed code $d"
        fi
    done

    if [ "$purge" = 1 ]; then
        # ensure_user ADOPTS a pre-existing nologin account named "bitpin" (it may belong to another
        # service on this shared server) and only records a marker when it created one itself.
        # Deleting an account this kit never created would orphan that service's files.
        local ours=0 panel_ours=0
        [ -f "$ETC_DIR/.user-created-by-installer" ] && ours=1
        [ -f "$PANEL_ETC_DIR/.user-created-by-installer" ] && panel_ours=1
        # shellcheck disable=SC2086  # PANEL_ACME_DIRS: two fixed paths (v3.8.2: the certificate's ACME work / logs)
        for d in "$PANEL_ETC_DIR" "$PANEL_STATE_DIR" $PANEL_ACME_DIRS; do
            if [ -e "$d" ]; then
                rm -rf "${d:?}"
                ok "removed $d"
            fi
        done
        if [ "$panel_ours" = 1 ] && getent passwd "$PANEL_USER" >/dev/null; then
            userdel "$PANEL_USER" 2>/dev/null || warn "could not delete user $PANEL_USER"
            getent group "$PANEL_USER" >/dev/null && { groupdel "$PANEL_USER" 2>/dev/null || true; }
            ok "removed user $PANEL_USER"
        fi
        for d in "$ETC_DIR" "$STATE_DIR" "$PAPER_STATE_DIR" "$NOTIFY_STATE_DIR" "$BACKUP_DIR"; do
            if [ -e "$d" ]; then
                rm -rf "${d:?}"
                ok "removed $d"
            fi
        done
        if getent passwd "$APP_USER" >/dev/null; then
            if [ "$ours" = 1 ]; then
                userdel "$APP_USER" 2>/dev/null || warn "could not delete user $APP_USER"
                ok "removed user $APP_USER"
                if getent group "$APP_GROUP" >/dev/null; then
                    groupdel "$APP_GROUP" 2>/dev/null || true
                fi
            else
                warn "user $APP_USER was NOT created by this installer (it already existed): left in place."
                warn "another service on this server may own it. Delete it by hand only if you are sure:  sudo userdel $APP_USER"
            fi
        fi
        echo
        echo "Purged. Remember to delete the API keys in the Bitpin panel and the Moonshot console."
    else
        echo
        echo "Kept (use --purge to delete): $ETC_DIR (contains your API keys and the Telegram token!), $STATE_DIR,"
        echo "$PAPER_STATE_DIR, $NOTIFY_STATE_DIR, $BACKUP_DIR and the user $APP_USER."
        echo "If this server will be given back or reused, run with --purge and delete the API keys"
        echo "in the Bitpin panel and the Moonshot console."
    fi
    echo "Your Bitpin account is unchanged: open positions and orders were not touched - including any RESTING"
    echo "limit orders the bot placed (crash-ladder bids, target sells): check the open orders in the Bitpin app."
}

main "$@"
