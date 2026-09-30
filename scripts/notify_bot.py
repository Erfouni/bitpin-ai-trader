"""Telegram notifier for the Bitpin bot - command line (bitpin/notify.py; a SEPARATE service that only
READS the trading bot's state directory; it never trades and never calls Bitpin).

  py scripts/notify_bot.py run      --config notify.json   service loop (bitpin-bot-notify.service)
  py scripts/notify_bot.py setup    [--config notify.json] after TELEGRAM_BOT_TOKEN is in the env and you
        sent /start to your bot: shows the bot's name and the chat ids that wrote to it (put yours into
        TELEGRAM_CHAT_ID)
  py scripts/notify_bot.py test     --config notify.json   sends ONE test message to every TELEGRAM_CHAT_ID
  py scripts/notify_bot.py status   --config notify.json   notifier state: cursors, queue, last error (no secrets)
  py scripts/notify_bot.py dry-run  --state-dir DIR [--mode paper|live] [--last N] [--at "YYYY-MM-DD HH:MM"]
        [--plain] [--translate]   renders the messages for the last N decisions / fill groups, the daily
        summary, /status and the alerts that would fire, WITHOUT sending anything (no token needed)
  py scripts/notify_bot.py apply-stop --config notify.json   the /stop relay's action (run by
        bitpin-bot-notify-stop.service as root with only CAP_DAC_OVERRIDE / CAP_CHOWN, no network: a UID
        the notifier cannot inspect through /proc): turns a confirmed Telegram /stop request in the
        notifier's state dir (a small regular file; symlinks / FIFOs are refused) into the bot's STOP
        kill-switch file, owned like the bot's directory

Environment (only these; /etc/bitpin-bot/notify.env on the server): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
(comma-separated numeric ids), TELEGRAM_HTTPS_PROXY (e.g. http://127.0.0.1:1081), optional KIMI_API_KEY
and KIMI_HTTPS_PROXY (Persian translation of Kimi's English reasoning). BITPIN_* keys are never read.

Exit codes: 0 ok, 1 error, 78 configuration / secret problem (the service does not restart on it).
"""
import argparse
import logging
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from bitpin import notify  # noqa: E402

log = logging.getLogger("bitpin.notify")


def _utf8_stdout():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def setup_logging(level="INFO"):
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(notify.RedactingFormatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
    root.addHandler(h)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def _config(args, **over):
    o = dict(over)
    if getattr(args, "state_dir", None):
        o["state_dir"] = args.state_dir
    if getattr(args, "notify_state_dir", None):
        o["notify_state_dir"] = args.notify_state_dir
    if getattr(args, "mode", None):
        o["mode"] = args.mode
    return notify.load_config(getattr(args, "config", None), overrides=o or None)


def _telegram(cfg, secrets):
    tg = cfg["telegram"]
    return notify.TelegramClient(secrets.token, proxy=secrets.telegram_proxy, api_base=tg["api_base"],
                                 timeout=tg["timeout_seconds"], max_retries=tg["max_retries"],
                                 backoff=tg["backoff_seconds"], backoff_max=tg["backoff_max_seconds"])


def _translator(cfg, secrets, notify_state_dir):
    if not cfg["translation"]["enabled"]:
        return None
    if not secrets.kimi_key:
        log.info("KIMI_API_KEY is not set in the notifier's env: Kimi's English reasoning is sent untranslated "
                 "(a Persian report_fa in the decision is still used)")
        return None
    return notify.Translator(cfg["translation"], secrets.kimi_key, secrets.kimi_proxy, notify_state_dir)


def _lock(path):
    """An exclusive lock file for the service loop (one notifier per state dir). Returns the open file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "a+")
    try:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:          # Windows
        import msvcrt
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            f.close()
            return None
    except OSError:
        f.close()
        return None
    return f


def cmd_run(args):
    gone = notify.drop_trading_secrets()
    if gone:
        log.warning("%s found in the notifier's environment and removed (never used). The notifier's unit must read "
                    "only /etc/bitpin-bot/notify.env", ", ".join(gone))
    cfg = _config(args)
    secrets = notify.load_secrets()
    os.makedirs(cfg["notify_state_dir"], exist_ok=True)
    lock = _lock(os.path.join(cfg["notify_state_dir"], notify.LOCK_FILE))
    if lock is None:
        log.error("another notifier is already running with %s", cfg["notify_state_dir"])
        return 1
    if not secrets.telegram_proxy:
        log.warning("TELEGRAM_HTTPS_PROXY is not set: Telegram is reached DIRECTLY (blocked on the server; set "
                    "TELEGRAM_HTTPS_PROXY=http://127.0.0.1:1081 in /etc/bitpin-bot/notify.env)")
    n = notify.Notifier(cfg, secrets, telegram=_telegram(cfg, secrets),
                        translator=_translator(cfg, secrets, cfg["notify_state_dir"]))
    log.info("notifier started: mode %s, bot state %s (read-only), own state %s, %d chat(s), Telegram via %s, "
             "commands %s, /stop %s", cfg["mode"], cfg["state_dir"], cfg["notify_state_dir"], len(secrets.chat_ids),
             notify.mask_proxy_url(secrets.telegram_proxy), "on" if cfg["commands_enabled"] else "off",
             "on (needs the relay units)" if cfg["allow_stop_command"] else "off")
    try:
        n.run(max_steps=1 if args.once else None)
    except KeyboardInterrupt:
        log.info("notifier stopped")
        return 0
    finally:
        try:
            n.state.save()
        except OSError:
            pass
    return 0


def cmd_setup(args):
    cfg = _config(args)
    secrets = notify.load_secrets(require_chats=False)
    tg = _telegram(cfg, secrets)
    print("Telegram route: %s" % notify.mask_proxy_url(secrets.telegram_proxy))
    try:
        me = tg.get_me()
    except notify.TelegramError as e:
        print("FAILED: %s" % e)
        if e.status == 401:
            print("  -> the token is wrong: copy it again from @BotFather into TELEGRAM_BOT_TOKEN")
        elif e.retryable:
            print("  -> Telegram is not reachable: is the proxy up? TELEGRAM_HTTPS_PROXY must be http://127.0.0.1:1081")
        return 1
    me = me if isinstance(me, dict) else {}
    safe = notify.term_safe        # every Telegram-supplied name: no control / bidi / invisible characters
    print("bot: @%s (%s)" % (safe(me.get("username"), 64) or "?", safe(me.get("first_name"), 64)))
    chats = {}
    try:
        updates = tg.get_updates(None, timeout=0)
    except notify.TelegramError as e:
        updates = []
        print("cannot read the messages sent to the bot: %s" % e)
        if e.status == 409:
            print("  -> HTTP 409: another program reads this bot's messages (the notifier service, or the same token "
                  "used elsewhere). Stop the service first: sudo systemctl stop bitpin-bot-notify")
    for u in updates or []:
        m = u.get("message") if isinstance(u, dict) else None
        if not isinstance(m, dict) or not isinstance(m.get("chat"), dict):
            continue
        c = m["chat"]
        if isinstance(c.get("id"), int) and not isinstance(c.get("id"), bool):
            chats[c["id"]] = (safe(c.get("type"), 20) or "?", safe(c.get("username") or c.get("title"), 40),
                              safe(c.get("first_name"), 40), m.get("date"))
    d = notify.read_json_file(os.path.join(cfg["notify_state_dir"], notify.STATE_FILE))
    seen = (d or {}).get("unknown_chats") if isinstance(d, dict) else None
    for cid, e in (seen if isinstance(seen, dict) else {}).items():
        e = e if isinstance(e, dict) else {}
        try:
            chats.setdefault(int(cid), (safe(e.get("type"), 20) or "?", safe(e.get("username"), 40), "", e.get("last")))
        except ValueError:
            continue
    if not chats:
        print("\nNo chat has written to the bot yet. Open your bot in Telegram (@%s), press START (or send /start), "
              "then run this again." % (safe(me.get("username"), 64) or "your bot"))
        return 1
    print("\nchats that wrote to the bot:")
    for cid, (typ, user, first, date) in sorted(chats.items()):
        when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(date)) \
            if isinstance(date, (int, float)) and notify.ts_ok(date) else "-"
        mark = "  (already in TELEGRAM_CHAT_ID)" if cid in secrets.chat_ids else ""
        group = "  <- a GROUP / CHANNEL: do not use (every member would see your balances)" \
            if cid < 0 or typ != "private" else ""
        print("  chat id %s  type %s  %s %s  last message %s%s%s" % (cid, typ, ("@" + user) if user else "", first,
                                                                     when, mark, group))
    private = sorted(cid for cid, (typ, _, _, _) in chats.items() if typ == "private" and cid > 0)
    if len(private) == 1:
        print("\nOnly one private chat wrote to the bot. If it is YOU (check the @username above), put this line into "
              "the env file:\n    TELEGRAM_CHAT_ID=%s\n"
              "then: sudo bitpin-bot notify-test   and   sudo systemctl enable --now bitpin-bot-notify" % private[0])
    elif private:
        print("\n%d private chats wrote to the bot - NOT all of them are you (anyone can write to a bot). Find YOUR line "
              "by your own @username (Telegram > Settings shows it) and put ONLY that number into the env file as "
              "TELEGRAM_CHAT_ID=<number>. Whoever is in TELEGRAM_CHAT_ID receives your balances and trades and can "
              "use /status and /stop." % len(private))
    else:
        print("\nNo private chat wrote to the bot yet: open the bot in Telegram yourself, press START (or send /start), "
              "then run this again. A group / channel id (negative) is never suggested.")
    return 0


def cmd_test(args):
    cfg = _config(args)
    secrets = notify.load_secrets()
    tg = _telegram(cfg, secrets)
    text = ("✅ <b>پیام آزمایشی</b> اطلاع‌رسان ربات بیت‌پین\n"
            "اگر این را می‌بینید، توکن، شناسهٔ چت و پراکسی درست تنظیم شده‌اند.\n🕒 %s" % notify.fmt_when(time.time()))
    bad = 0
    for cid in secrets.chat_ids:
        try:
            tg.send_message(cid, text)
            print("chat %s: OK" % cid)
        except notify.TelegramError as e:
            bad += 1
            print("chat %s: FAILED: %s" % (cid, e))
            if e.status == 400:
                print("  -> 'chat not found': send /start to the bot from that account first, and check the id")
            elif e.status == 403:
                print("  -> the bot is blocked by that account or is not a member of that group")
            elif e.status == 401:
                print("  -> wrong token")
            elif e.retryable:
                print("  -> Telegram not reachable: is the proxy (TELEGRAM_HTTPS_PROXY) up?")
    return 1 if bad else 0


def cmd_status(args):
    cfg = _config(args)
    print(notify.status_report(cfg))
    return 0


def _parse_at(s):
    if not s:
        return None
    dt = datetime.strptime(s.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=notify.TEHRAN)
    return dt.timestamp()


def cmd_dry_run(args):
    cfg = _config(args, notify_state_dir=args.notify_state_dir or os.path.join(
        os.path.abspath(args.state_dir or "."), ".notify-dry-run-unused"))
    translator = None
    if args.translate:
        secrets = notify.load_secrets(require_token=False, require_chats=False)
        translator = _translator(cfg, secrets, None)
    out = notify.dry_run(cfg, last=args.last, now=_parse_at(args.at), translator=translator)
    for title, text in out:
        chunks = notify.split_message(text)
        print("=" * 20 + " %s  (%d chars, %d message%s)" % (title, len(text), len(chunks), "s" if len(chunks) > 1 else ""))
        print(notify.strip_html(text) if args.plain else text)
        print()
    return 0


def cmd_apply_stop(args):
    cfg = _config(args)
    ok, detail = notify.apply_stop_request(cfg["state_dir"], cfg["notify_state_dir"],
                                           max_age=cfg["stop_request_max_age_seconds"])
    print(detail)
    return 0 if ok or detail == "no stop request" else 1


def main(argv=None):
    _utf8_stdout()
    p = argparse.ArgumentParser(description="Telegram notifier for the Bitpin bot (read-only; never trades)")
    sub = p.add_subparsers(dest="cmd")
    specs = {"run": cmd_run, "setup": cmd_setup, "test": cmd_test, "status": cmd_status, "dry-run": cmd_dry_run,
             "apply-stop": cmd_apply_stop}
    for name in specs:
        s = sub.add_parser(name)
        s.add_argument("--config", help="notify.json (default: built-in defaults)")
        s.add_argument("--state-dir", help="the trading bot's state dir (read-only), overrides the config")
        s.add_argument("--notify-state-dir", help="the notifier's own state dir, overrides the config")
        s.add_argument("--mode", choices=("live", "paper"), help="which bot files to read (default: config)")
        s.add_argument("--log-level", default="INFO")
        if name == "run":
            s.add_argument("--once", action="store_true", help="one iteration, then exit (for testing)")
        if name == "dry-run":
            s.add_argument("--last", type=int, default=3, help="decisions and fill groups to render (default 3)")
            s.add_argument("--at", help='render as of this Tehran time, "YYYY-MM-DD HH:MM" (default: now)')
            s.add_argument("--plain", action="store_true", help="print without HTML tags")
            s.add_argument("--translate", action="store_true",
                           help="translate via Kimi (needs KIMI_API_KEY; one real Moonshot call per decision)")
    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 1
    setup_logging(args.log_level)
    try:
        return specs[args.cmd](args)
    except notify.NotifyConfigError as e:
        log.error("configuration: %s", e)
        return notify.EXIT_CONFIG
    except notify.TelegramError as e:
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
