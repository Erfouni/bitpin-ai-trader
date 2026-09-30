# shellcheck shell=bash
# Shared helpers for deploy/install.sh, deploy/update.sh and deploy/uninstall.sh.
# This file is sourced, never executed. Everything here assumes bash 4+, GNU coreutils/tar and systemd
# (Ubuntu 22.04 / 24.04).

APP_USER="bitpin"
APP_GROUP="bitpin"
APP_DIR="/opt/bitpin-bot"
ETC_DIR="/etc/bitpin-bot"
ENV_FILE="$ETC_DIR/bitpin-bot.env"
STATE_DIR="/var/lib/bitpin-bot"
PAPER_STATE_DIR="/var/lib/bitpin-bot-paper"
BACKUP_DIR="/var/backups/bitpin-bot"
UNIT_DIR="/etc/systemd/system"
UNITS="bitpin-bot.service bitpin-bot-paper.service"
# The Telegram notifier (docs/TELEGRAM_FA.md). NOT in $UNITS on purpose: $UNITS drives running_units() (install.sh's
# "use update.sh" guard, update.sh's stop / restart / auto-rollback), and the notifier must never block an install
# or cause a rollback of the trading bot.
NOTIFY_ENV_FILE="$ETC_DIR/notify.env"
NOTIFY_CONFIG="$ETC_DIR/notify.json"
NOTIFY_STATE_DIR="/var/lib/bitpin-bot-notify"
NOTIFY_UNITS="bitpin-bot-notify.service bitpin-bot-notify-stop.service bitpin-bot-notify-stop.path"
# Release v3 ops units (root only, no trading, no network): the OnFailure reporter template that
# bitpin-bot.service names through OnFailure= (exit status 78 -> a Telegram alert via the notifier's relay
# file) and the daily state backup service + timer. Installed leniently like the notifier's units (a
# rollback may restore a tree without them) and never in $UNITS (no stop / restart / rollback logic).
OPS_UNITS="bitpin-bot-failed@.service bitpin-bot-backup.service bitpin-bot-backup.timer"
# Release v3.1: the management panel (docs/PANEL_FA.md). Installed leniently like the ops units and NEVER enabled
# by install.sh / update.sh: it opens a port, so only 'sudo bitpin-bot panel-setup' enables it. Its own user
# bitpin-panel and /etc/bitpin-bot-panel are created there too.
PANEL_UNITS="bitpin-bot-panel.service bitpin-bot-panel-helper.socket bitpin-bot-panel-helper@.service"
PANEL_ETC_DIR="/etc/bitpin-bot-panel"
PANEL_STATE_DIR="/var/lib/bitpin-bot-panel"
PANEL_USER="bitpin-panel"
# System configuration files of v3 (year_review/Y2-ops-12-months), both scoped to the bot: NEEDRESTART
# excludes the two trading units from needrestart's automatic restarts after apt upgrades, LOGROTATE rotates
# bot_live.log by size with copytruncate. Nothing system-wide: the journal settings of other
# services on the server are never touched (v3 security review). Their paths hang off the parent of $ETC_DIR (/etc) so the
# deploy tests, which point ETC_DIR into a temp directory, never touch the real /etc: ops_conf_paths prints
# "SOURCE_NAME DESTINATION" lines.
ops_conf_paths() {
    local sys
    sys="$(dirname "$ETC_DIR")"
    printf '%s\n' "needrestart-bitpin-bot.conf $sys/needrestart/conf.d/bitpin-bot.conf" \
                   "logrotate-bitpin-bot $sys/logrotate.d/bitpin-bot"
}
PYTHON="/usr/bin/python3"
CLI_LINK="/usr/local/bin/bitpin-bot"
STAMP="$(date -u +%Y%m%d-%H%M%S)"

info() { printf '[ .. ] %s\n' "$*"; }
ok()   { printf '[ OK ] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
die()  { printf '[FAIL] %s\n' "$*" >&2; exit 1; }

# The bot's own RESTING limit orders (crash-ladder bids, target sells) the live order journal shows as
# resting / being sent / of unknown outcome ("?" = the journal cannot be read). Used by update.sh --rollback and
# uninstall.sh: both would leave such orders on Bitpin with nothing that manages or can cancel them.
resting_count() {  # STATE_DIR
    [ -f "$1/live_orders.json" ] || { echo 0; return 0; }
    "$PYTHON" - "$1/live_orders.json" <<'PY' 2>/dev/null || echo "?"
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        orders = json.load(f).get("orders") or {}
    print(sum(1 for e in orders.values() if isinstance(e, dict) and e.get("kind") == "limit"
              and e.get("status") in ("resting", "submitting", "unknown")))
except (OSError, ValueError, AttributeError):
    print("?")
PY
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "run this with sudo, e.g.: sudo bash $0"
}

check_systemd() {
    [ -d /run/systemd/system ] || die "systemd is not running on this machine (containers / WSL without systemd are not supported)"
    command -v systemctl >/dev/null 2>&1 || die "systemctl not found"
    command -v runuser >/dev/null 2>&1 || die "runuser not found (package util-linux)"
}

check_source_tree() {  # DIR
    [ -f "$1/scripts/run_bot.py" ] && [ -d "$1/bitpin" ] && [ -d "$1/deploy" ] \
        || die "$1 does not look like the bot project (need scripts/run_bot.py, bitpin/, deploy/). Run the script from the unpacked project directory."
}

check_os() {  # FORCE(0/1)
    [ -r /etc/os-release ] || die "cannot read /etc/os-release"
    local id ver
    id="$(. /etc/os-release && echo "${ID:-}")"
    ver="$(. /etc/os-release && echo "${VERSION_ID:-}")"
    if [ "$id" != "ubuntu" ]; then
        if [ "${1:-0}" = 1 ]; then
            warn "this is not Ubuntu (${id:-?} ${ver:-?}); continuing because of --force"
        else
            die "this installer is written for Ubuntu (found: ${id:-?} ${ver:-?}). Use --force to try anyway."
        fi
    else
        case "$ver" in
            2[2-9].*|[3-9][0-9].*) ok "Ubuntu $ver" ;;
            *) warn "Ubuntu $ver detected: 22.04 or 24.04 is recommended" ;;
        esac
    fi
}

check_python() {
    [ -x "$PYTHON" ] || die "$PYTHON not found. Install it with: sudo apt update && sudo apt install -y python3"
    local v
    v="$("$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')" || die "$PYTHON does not run"
    "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
        || die "$PYTHON is version $v; the bot needs Python 3.8 or newer (Ubuntu 22.04 has 3.10, 24.04 has 3.12)"
    "$PYTHON" -c 'import ssl, json, decimal, argparse, urllib.request, http.client' \
        || die "$PYTHON is missing standard modules (ssl/json/...). Try: sudo apt install -y python3"
    ok "python3 $v at $PYTHON"
    if [ ! -s /etc/ssl/certs/ca-certificates.crt ]; then
        warn "CA certificate bundle missing: HTTPS will fail. Fix: sudo apt install -y ca-certificates"
    fi
}

# ---------------------------------------------------------------------------------------------- code

# Copy the project from SRC into STAGE, leaving out caches, local state, research data, scratch
# output and anything that looks like a credential file.
stage_code() {  # SRC STAGE WITH_TESTS(0/1) WITH_DATA(0/1)
    local src="$1" stage="$2" with_tests="$3" with_data="$4"
    local -a ex=(
        --exclude-vcs
        --exclude='./state' --exclude='./scratch' --exclude='./.venv' --exclude='./venv'
        --exclude='./.idea' --exclude='./.vscode' --exclude='./.claude' --exclude='./.pytest_cache'
        --exclude='./.mypy_cache' --exclude='./.ruff_cache'
        --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' --exclude='*.log' --exclude='*.lock'
        --exclude='./config.json' --exclude='./kimi.json' --exclude='*.env' --exclude='.env'
        --exclude='./api.txt' --exclude='*keys*.txt' --exclude='*.key' --exclude='*.pem'
        --exclude='bitpin_token.json' --exclude='*.bak-*' --exclude='*.tar.gz' --exclude='*.zip'
        # scripts/kimi_check.py is the standalone research tool used while gathering the server facts.
        # Run without KIMI_API_KEY it PROMPTS for the key and writes it in cleartext next to itself
        # (scripts/kimi.key), which would be a second, undocumented place where the key lives - the env
        # file says it is kept only there. 'bitpin-bot kimi-check' supersedes it, so it is never shipped.
        --exclude='./scripts/kimi_check.py'
        # v3: the research notebooks / data and the development notes and review findings are not code
        # the server needs (research/ alone is several MB of CSV and JSON; the review JSON quotes Kimi
        # replies). The bot's prompt template bitpin/prompt_template.txt IS shipped (checked below).
        --exclude='./research' --exclude='./docs/dev_notes' --exclude='./docs/reviews'
    )
    [ "$with_tests" = 1 ] || ex+=(--exclude='./tests')
    [ "$with_data" = 1 ] || ex+=(--exclude='./data')
    rm -rf "${stage:?}"
    mkdir -p "$stage"
    tar -C "$src" "${ex[@]}" -cf - . | tar -C "$stage" --no-same-owner --no-same-permissions -xf -
    [ -f "$stage/scripts/run_bot.py" ] || die "copy failed: $stage/scripts/run_bot.py missing"
    # the Kimi system prompt is a data file next to the code (v3): a copy without it starts and exits 78
    if [ -f "$src/bitpin/prompt_template.txt" ] && [ ! -f "$stage/bitpin/prompt_template.txt" ]; then
        die "copy failed: $stage/bitpin/prompt_template.txt (the Kimi prompt template) missing"
    fi
    # the management panel's stylesheet (v3.2) is a data file next to the code as well
    if [ -f "$src/bitpin/static/panel.css" ] && [ ! -f "$stage/bitpin/static/panel.css" ]; then
        die "copy failed: $stage/bitpin/static/panel.css (the panel's stylesheet) missing"
    fi
}

# Compile every .py file with the server's python (no .pyc written) so code that needs a newer
# Python than the one installed is caught before it replaces the running version.
syntax_check() {  # DIR
    "$PYTHON" - "$1" <<'PY'
import os
import sys

root = sys.argv[1]
bad = 0
for d, dirs, files in os.walk(root):
    dirs[:] = [x for x in dirs if x != "__pycache__"]
    for f in files:
        if f.endswith(".py"):
            p = os.path.join(d, f)
            try:
                with open(p, "rb") as fh:
                    compile(fh.read(), p, "exec", dont_inherit=True)
            except (SyntaxError, ValueError) as e:
                bad += 1
                print("[FAIL] %s: %s" % (os.path.relpath(p, root), e))
if bad:
    print("[FAIL] %d file(s) do not compile with Python %d.%d" % (bad, sys.version_info[0], sys.version_info[1]))
sys.exit(1 if bad else 0)
PY
}

# root-owned, world-readable code; shell scripts with Unix line endings and executable.
normalize_tree() {  # DIR
    local d="$1" f
    find "$d" -type d -exec chmod 0755 {} +
    find "$d" -type f -exec chmod 0644 {} +
    for f in "$d"/deploy/*.sh "$d"/deploy/bitpin-bot "$d"/deploy/*.service "$d"/deploy/*.path "$d"/deploy/*.example \
             "$d"/deploy/check_server.py "$d"/deploy/apply_profile.py "$d"/deploy/*.timer "$d"/deploy/*.conf \
             "$d"/deploy/logrotate-* "$d"/deploy/bitpin-bot-failed "$d"/deploy/bitpin-bot-backup; do
        [ -f "$f" ] && sed -i 's/\r$//' "$f"
    done
    for f in "$d"/deploy/*.sh "$d"/deploy/bitpin-bot "$d"/deploy/check_server.py "$d"/deploy/apply_profile.py \
             "$d"/deploy/bitpin-bot-failed "$d"/deploy/bitpin-bot-backup; do
        [ -f "$f" ] && chmod 0755 "$f"
    done
    chown -R root:root "$d"
}

compile_tree() {  # DIR  (byte-code cache for faster starts; failures are harmless)
    "$PYTHON" -m compileall -q "$1/bitpin" "$1/scripts" "$1/deploy" >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------------------------- system

ensure_user() {
    if ! getent group "$APP_GROUP" >/dev/null; then
        groupadd --system "$APP_GROUP"
        ok "created group $APP_GROUP"
    fi
    if ! getent passwd "$APP_USER" >/dev/null; then
        useradd --system --gid "$APP_GROUP" --home-dir "$STATE_DIR" --no-create-home \
                --shell /usr/sbin/nologin --comment "Bitpin trading bot" "$APP_USER"
        ok "created system user $APP_USER (no login shell)"
        # remembered so that uninstall.sh --purge deletes ONLY an account this kit created (the
        # branch below deliberately adopts a pre-existing nologin "bitpin" of another service)
        mkdir -p "$ETC_DIR" 2>/dev/null || true
        : > "$ETC_DIR/.user-created-by-installer" 2>/dev/null || true
    else
        # The server may run other services and have other accounts: an existing "bitpin" user that
        # can log in is somebody else's account and is NEVER modified (no shell / group change).
        local sh
        sh="$(getent passwd "$APP_USER" | cut -d: -f7)"
        case "$sh" in
            */nologin|*/false) ok "system user $APP_USER exists" ;;
            *) die "a user named '$APP_USER' already exists and can log in (shell $sh): it looks like a person's or another service's account, and this installer does not modify it. The bot needs its own user '$APP_USER' without a login shell. Ask the server administrator; nothing was changed." ;;
        esac
    fi
}

ensure_dirs() {
    local d
    for d in "$STATE_DIR" "$PAPER_STATE_DIR"; do
        install -d -m 0700 -o "$APP_USER" -g "$APP_GROUP" "$d"
        chown -R "$APP_USER:$APP_GROUP" "$d"
        chmod 0700 "$d"
    done
    # the notifier's OWN state dir (its unit's StateDirectory= would create it too, but the /stop relay's path
    # unit watches a file in it before the notifier first runs). The notifier never writes the bot's dirs.
    install -d -m 0700 -o "$APP_USER" -g "$APP_GROUP" "$NOTIFY_STATE_DIR"
    chown -R "$APP_USER:$APP_GROUP" "$NOTIFY_STATE_DIR"
    chmod 0700 "$NOTIFY_STATE_DIR"
    install -d -m 0750 -o root -g "$APP_GROUP" "$ETC_DIR"
    chown root:"$APP_GROUP" "$ETC_DIR"
    chmod 0750 "$ETC_DIR"
    install -d -m 0700 -o root -g root "$BACKUP_DIR"
    ok "directories: $STATE_DIR, $PAPER_STATE_DIR, $NOTIFY_STATE_DIR (0700 $APP_USER), $ETC_DIR (0750 root:$APP_GROUP)"
}

# Install TEMPLATE as DST only if DST does not exist yet; always (re)apply owner and mode.
# GROUP (default $APP_GROUP): the secrets files (bitpin-bot.env, notify.env) are root:root 0600 - no process of
# user bitpin ever opens them (systemd reads EnvironmentFile= as root, the helper loads them as root before
# runuser), so neither service can read the other one's secrets file.
install_if_absent() {  # TEMPLATE DST MODE [GROUP]
    local src="$1" dst="$2" mode="$3" grp="${4:-$APP_GROUP}" tmp
    if [ -e "$dst" ]; then
        chown root:"$grp" "$dst"
        chmod "$mode" "$dst"
        ok "kept existing $dst (mode $mode root:$grp)"
        return 0
    fi
    tmp="$(mktemp "$ETC_DIR/.tmp.XXXXXX")"
    sed 's/\r$//' "$src" > "$tmp"
    chown root:"$grp" "$tmp"
    chmod "$mode" "$tmp"
    mv -f "$tmp" "$dst"
    ok "installed $dst from $(basename "$src") (mode $mode root:$grp)"
}

# The Kimi settings example is created by the brain integration; accept a few locations.
find_kimi_example() {  # APPDIR
    local c
    for c in "$1/kimi.example.json" "$1/deploy/kimi.example.json" "$1/config/kimi.example.json"; do
        if [ -f "$c" ]; then echo "$c"; return 0; fi
    done
    return 1
}

install_config_files() {  # APPDIR
    local app="$1" kex
    install_if_absent "$app/deploy/bitpin-bot.env.example" "$ENV_FILE" 0600 root
    install_if_absent "$app/config.example.json" "$ETC_DIR/config.json" 0640
    if kex="$(find_kimi_example "$app")"; then
        install_if_absent "$kex" "$ETC_DIR/kimi.json" 0640
    elif [ -e "$ETC_DIR/kimi.json" ]; then
        chown root:"$APP_GROUP" "$ETC_DIR/kimi.json"
        chmod 0640 "$ETC_DIR/kimi.json"
        ok "kept existing $ETC_DIR/kimi.json (mode 0640 root:$APP_GROUP)"
    else
        warn "no kimi.example.json in this version of the code: $ETC_DIR/kimi.json was NOT created."
        warn "the live service needs it (--kimi-config). Install a version with the Kimi brain, then re-run install.sh."
    fi
    # Telegram notifier: its own secrets file (TELEGRAM_BOT_TOKEN ...; never the trading keys) and settings.
    # Guarded: restore_backup may run this with an OLDER tree that does not have them.
    if [ -f "$app/deploy/notify.env.example" ]; then
        install_if_absent "$app/deploy/notify.env.example" "$NOTIFY_ENV_FILE" 0600 root
    fi
    if [ -f "$app/notify.example.json" ]; then
        install_if_absent "$app/notify.example.json" "$NOTIFY_CONFIG" 0640
    fi
}

# Install one unit file; sets UNITS_CHANGED=1 if it changed. The previous copy goes to $BACKUP_DIR.
UNITS_CHANGED=0
install_unit() {  # SRC_FILE UNIT_NAME
    local src="$1" dst="$UNIT_DIR/$2" tmp
    tmp="$(mktemp)"
    sed 's/\r$//' "$src" > "$tmp"
    if [ -f "$dst" ] && cmp -s "$tmp" "$dst"; then
        rm -f "$tmp"
        return 0
    fi
    if [ -f "$dst" ]; then
        install -d -m 0700 "$BACKUP_DIR"
        cp -p "$dst" "$BACKUP_DIR/$2.$STAMP"
        warn "replaced $dst (old copy: $BACKUP_DIR/$2.$STAMP). Keep your own changes in drop-ins: sudo systemctl edit ${2%.service}"
    fi
    install -m 0644 -o root -g root "$tmp" "$dst"
    rm -f "$tmp"
    UNITS_CHANGED=1
    ok "installed $dst"
}

install_units() {  # DEPLOY_DIR  -> runs daemon-reload when something changed
    local u
    UNITS_CHANGED=0
    for u in $UNITS; do
        [ -f "$1/$u" ] || die "missing unit file $1/$u"
        install_unit "$1/$u" "$u"
    done
    # the notifier's units LENIENTLY: restore_backup installs the units of an OLDER tree that may not have them
    for u in $NOTIFY_UNITS; do
        if [ -f "$1/$u" ]; then
            install_unit "$1/$u" "$u"
        else
            warn "no $u in this version (Telegram notifier not installed)"
        fi
    done
    # the v3 ops units the same way (a tree from before v3 has none)
    for u in $OPS_UNITS; do
        if [ -f "$1/$u" ]; then
            install_unit "$1/$u" "$u"
        else
            warn "no $u in this version (v3 ops unit not installed)"
        fi
    done
    # the v3.1 panel units the same way (installed, never enabled here)
    for u in $PANEL_UNITS; do
        if [ -f "$1/$u" ]; then
            install_unit "$1/$u" "$u"
        fi
    done
    if [ "$UNITS_CHANGED" = 1 ]; then
        systemctl daemon-reload
        ok "systemd daemon-reload done"
    else
        ok "systemd units unchanged"
    fi
}

# One system configuration file (needrestart / logrotate), CRLF-stripped, 0644, only
# when its content changed (idempotent: a re-run prints nothing and restarts nothing). Returns 0 when it
# was written, 1 when unchanged or absent. Owner root:root only when running as root (the deploy tests
# run this as a normal user into a temp directory).
install_conf_file() {  # SRC DST
    local src="$1" dst="$2" tmp
    [ -f "$src" ] || return 1
    tmp="$(mktemp)"
    sed 's/\r$//' "$src" > "$tmp"
    if [ -f "$dst" ] && cmp -s "$tmp" "$dst"; then
        rm -f "$tmp"
        return 1
    fi
    install -d -m 0755 "$(dirname "$dst")"
    if [ "$(id -u)" -eq 0 ]; then
        install -m 0644 -o root -g root "$tmp" "$dst"
    else
        install -m 0644 "$tmp" "$dst"
    fi
    rm -f "$tmp"
    ok "installed $dst"
    return 0
}

# The v3 system files (ops_conf_paths) from DEPLOY_DIR: needrestart exclusion, logrotate. Both are read at
# their next run (nothing to restart). A tree from before v3 has none of them: warned, nothing removed
# (uninstall.sh removes them).
install_ops_files() {  # DEPLOY_DIR
    local name dst
    while read -r name dst; do
        [ -n "$name" ] || continue
        if [ ! -f "$1/$name" ]; then
            warn "no $name in this version (not installed)"
            continue
        fi
        install_conf_file "$1/$name" "$dst" || true
    done <<< "$(ops_conf_paths)"
}

# The daily state backup timer: the ONE unit the installer enables (root only, no trading, no network; a
# missed run happens at the next boot). Nothing else is ever enabled or started by install.sh.
enable_backup_timer() {
    [ -f "$UNIT_DIR/bitpin-bot-backup.timer" ] || return 0
    if systemctl enable --now bitpin-bot-backup.timer >/dev/null 2>&1; then
        ok "daily state backup enabled: bitpin-bot-backup.timer (03:10 UTC -> $BACKUP_DIR/state-<date>.tgz, 14 kept)"
    else
        warn "could not enable bitpin-bot-backup.timer (sudo systemctl enable --now bitpin-bot-backup.timer)"
    fi
}

# Removal of the v3 system files (uninstall.sh); both are read at their next run.
remove_ops_files() {
    local name dst
    while read -r name dst; do
        [ -n "$name" ] || continue
        if [ -f "$dst" ]; then
            rm -f "$dst"
            ok "removed $dst"
        fi
    done <<< "$(ops_conf_paths)"
}

install_cli_link() {
    ln -sfn "$APP_DIR/deploy/bitpin-bot" "$CLI_LINK"
    ok "helper command installed: $CLI_LINK -> $APP_DIR/deploy/bitpin-bot (use: sudo bitpin-bot help)"
}

# Verify that the installed run_bot.py understands the flags the units use.
check_cli_flags() {
    local out missing="" f
    out="$(cd "$APP_DIR" && runuser -u "$APP_USER" -- env PYTHONDONTWRITEBYTECODE=1 "$PYTHON" "$APP_DIR/scripts/run_bot.py" live --help 2>&1)" || true
    for f in --brain --kimi-config --non-interactive; do
        case "$out" in *"$f"*) ;; *) missing="$missing live:$f" ;; esac
    done
    out="$(cd "$APP_DIR" && runuser -u "$APP_USER" -- env PYTHONDONTWRITEBYTECODE=1 "$PYTHON" "$APP_DIR/scripts/run_bot.py" paper --help 2>&1)" || true
    for f in --brain --kimi-config --capital-irt; do
        case "$out" in *"$f"*) ;; *) missing="$missing paper:$f" ;; esac
    done
    out="$(cd "$APP_DIR" && runuser -u "$APP_USER" -- env PYTHONDONTWRITEBYTECODE=1 "$PYTHON" "$APP_DIR/scripts/run_bot.py" --help 2>&1)" || true
    case "$out" in *confirm-live*) ;; *) missing="$missing confirm-live" ;; esac
    # "sudo bitpin-bot confirm-live" passes the service's config files, like ExecStart
    out="$(cd "$APP_DIR" && runuser -u "$APP_USER" -- env PYTHONDONTWRITEBYTECODE=1 "$PYTHON" "$APP_DIR/scripts/run_bot.py" confirm-live --help 2>&1)" || true
    for f in --config --kimi-config; do
        case "$out" in *"$f"*) ;; *) missing="$missing confirm-live:$f" ;; esac
    done
    if [ -n "$missing" ]; then
        warn "this version of scripts/run_bot.py lacks what the services need:$missing"
        warn "bitpin-bot.service would fail to start. Install a version with the Kimi brain integration before enabling it."
        return 1
    fi
    ok "run_bot.py supports live/paper --brain kimi --kimi-config, --non-interactive and confirm-live --config --kimi-config"
    return 0
}

check_time() {  # ENABLE_NTP(0/1)
    local tz synced active="" s
    if ! command -v timedatectl >/dev/null 2>&1; then
        warn "timedatectl not found: cannot check time synchronisation"
        return 0
    fi
    tz="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
    info "system timezone: ${tz:-unknown}. Not changed: the units run the bot with TZ=UTC and it works in UTC internally."
    for s in systemd-timesyncd chrony chronyd ntp ntpsec openntpd; do
        if systemctl is-active --quiet "$s" 2>/dev/null; then active="$active $s"; fi
    done
    if [ -z "$active" ] && [ "${1:-0}" = 1 ]; then
        info "enabling NTP time synchronisation (timedatectl set-ntp true)"
        if timedatectl set-ntp true 2>/dev/null; then
            sleep 2
            systemctl is-active --quiet systemd-timesyncd 2>/dev/null && active=" systemd-timesyncd"
        else
            warn "could not enable NTP. Install a time sync client: sudo apt install -y systemd-timesyncd"
        fi
    fi
    synced="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)"
    if [ -n "$active" ] && [ "$synced" = "yes" ]; then
        ok "time sync active (${active# }) and the clock is synchronised"
    elif [ -n "$active" ]; then
        warn "time sync service running (${active# }) but the clock is not synchronised yet."
        warn "if this persists, tell the server administrator (the NTP servers may be unreachable from this network). This installer never changes the system clock settings."
    else
        warn "no time synchronisation service is active. The bot needs an accurate clock (candle timing, API tokens)."
        warn "tell the server administrator (on a shared server, do not change system settings yourself). On a server you administer alone: re-run install.sh with --enable-ntp"
    fi
}

running_units() {  # prints the bot units that are LIVE right now (systemd owns them)
    # NOT `systemctl is-active --quiet`: that is non-zero for a unit in "activating (auto-restart)",
    # and with Restart=always / RestartSec=60 / RestartMaxDelaySec=900 a unit that fails at every
    # start spends almost all of its backoff in exactly that state - the state `bitpin-bot health`
    # reports as RESTART LOOP. install.sh would then bypass its "use update.sh" guard and replace a
    # LIVE bot's code untested, and update.sh would skip the live-confirmation check, swap the tree
    # under the unit and exit 0 while systemd starts the new code minutes later with no rollback.
    # Same states as refuse_if_live_running in deploy/bitpin-bot.
    local u st out=""
    for u in $UNITS; do
        st="$(systemctl show -p ActiveState --value "$u" 2>/dev/null)"
        case "$st" in
            active|activating|reloading|deactivating) out="$out $u" ;;
        esac
    done
    echo "${out# }"
}
