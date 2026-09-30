#!/usr/bin/env python3
"""Set up the management panel (release v3.1; docs/PANEL_FA.md). Run as root through the command line:

    sudo bitpin-bot panel-setup       first setup: the user bitpin-panel, a username, a strong password, the
                                      authenticator code (2FA, recommended), a self-signed TLS certificate,
                                      /etc/bitpin-bot-panel/panel.json - then the panel is started
    sudo bitpin-bot panel-password    a new password (--username: a new username too); open sessions end
    sudo bitpin-bot panel-totp        the authenticator code on (a new secret) or off (--off)
    sudo bitpin-bot panel-status      address, certificate fingerprint, 2FA, services

The password is never stored: panel.json keeps its PBKDF2 hash. The authenticator secret is shown ONCE, in
this terminal, to be added to an authenticator app (Google Authenticator, Aegis, ...). Opening the port in
a firewall is left to the owner (the command is printed).
"""
import argparse
import getpass
import hashlib
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PANEL_USER = "bitpin-panel"
PANEL_ETC = "/etc/bitpin-bot-panel"
USER_MARKER = ".user-created-by-installer"
UNITS = ("bitpin-bot-panel-helper.socket", "bitpin-bot-panel.service")
UNIT_DIR = "/etc/systemd/system"
DEFAULT_PORT = 8443
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
WEAK_USERNAMES = ("admin", "root", "user", "test", "bitpin", "panel", "administrator", "owner")


class SetupError(Exception):
    pass


def conf_path(etc=PANEL_ETC):
    return os.path.join(etc, "panel.json")


def default_conf(etc=PANEL_ETC):
    return {"bind": "0.0.0.0", "port": DEFAULT_PORT,
            "tls_cert": etc + "/tls/cert.pem", "tls_key": etc + "/tls/key.pem",
            "username": None, "password_hash": None, "totp_secret": None, "allowed_hosts": [],
            "session_idle_minutes": 30, "session_max_hours": 12, "helper_socket": "/run/bitpin-panel/helper.sock",
            "audit_log": "/var/lib/bitpin-bot-panel/audit.jsonl", "trusted_proxy": False}


def username_problem(name):
    if not isinstance(name, str) or not _USERNAME_RE.match(name):
        return "3-32 characters: letters, digits, . _ -"
    if name.lower() in WEAK_USERNAMES:
        return "too easy to guess: pick a less common username"
    return None


def cert_fingerprint(pem_text):
    """SHA-256 fingerprint of a PEM certificate as a browser shows it (AA:BB:...)."""
    der = ssl.PEM_cert_to_DER_cert(pem_text)
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def server_addresses(run=subprocess.run):
    """The server's own addresses (hostname -I), IPv4 first; loopback and link-local left out."""
    try:
        out = run(["hostname", "-I"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10).stdout
        addrs = out.decode("ascii", "replace").split()
    except (OSError, subprocess.SubprocessError):
        addrs = []
    good = [a for a in addrs if not a.startswith(("127.", "fe80:", "::1"))]
    return sorted(set(good), key=lambda a: (":" in a, good.index(a)))


def openssl_argv(key_path, cert_path, cn, addrs, hostname):
    san = ["IP:%s" % a for a in addrs]
    if hostname and re.match(r"^[A-Za-z0-9.-]{1,253}$", hostname):
        san.append("DNS:%s" % hostname)
    argv = ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
            "-days", "825", "-sha256", "-subj", "/CN=%s" % cn, "-keyout", key_path, "-out", cert_path]
    if san:
        argv += ["-addext", "subjectAltName=" + ",".join(san)]
    return argv


def build_conf(old, username=None, password_hash=None, totp_secret=False, port=None, etc=PANEL_ETC):
    """panel.json content: the old values kept, the given ones replaced (totp_secret False = unchanged)."""
    conf = default_conf(etc)
    conf.update(dict((k, v) for k, v in (old or {}).items() if k in conf))
    if username is not None:
        conf["username"] = username
    if password_hash is not None:
        conf["password_hash"] = password_hash
    if totp_secret is not False:
        conf["totp_secret"] = totp_secret
    if port is not None:
        conf["port"] = int(port)
    return conf


def write_conf(conf, path, gid=None):
    """Atomic, 0640, root:<gid> (the panel's group reads it; only root writes it)."""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".panel.json.", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(conf, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.chmod(tmp, 0o640)
        if gid is not None and hasattr(os, "chown"):
            os.chown(tmp, 0, gid)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_conf(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except FileNotFoundError:
        return {}
    except ValueError as e:
        raise SetupError("%s is not valid JSON (%s): fix or delete it, then run this again" % (path, e))


# ---------------------------------------------------------------- the questions
def ask_username(ask, current=None):
    while True:
        name = ask("Panel username%s: " % (" [%s]" % current if current else "")).strip() or (current or "")
        why = username_problem(name)
        if why is None:
            return name
        print("  refused: %s" % why)


def ask_password(secret_ask, username, auth):
    while True:
        pw = secret_ask("New panel password (12+ characters, at least 3 of: lower, upper, digits, symbols): ")
        problems = auth.password_problems(pw, username)
        if problems:
            for p in problems:
                print("  refused: %s" % p)
            continue
        if secret_ask("The same password again: ") != pw:
            print("  refused: the two passwords differ")
            continue
        return auth.hash_password(pw)


def ask_totp(ask, username, auth, clock, confirm_first=True):
    """A new authenticator secret, confirmed with one code from the app (None when the owner says no)."""
    if confirm_first:
        yn = ask("Use an authenticator code (2FA, strongly recommended for a panel on the internet)? [Y/n] ")
        if yn.strip().lower() in ("n", "no"):
            print("  2FA OFF: the panel is protected by the password alone.")
            return None
    secret = auth.new_totp_secret()
    print()
    print("  Add this key to an authenticator app (Google Authenticator, Aegis, 2FAS, ...):")
    print("      key : %s" % " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)))
    print("      type: time-based, 6 digits, 30 seconds (the app's default)")
    print("      or as a link: %s" % auth.otpauth_uri(secret, username, issuer="bitpin-bot"))
    print("  It is shown only now. With it and the password anyone can log in: keep it private.")
    print()
    for _ in range(5):
        code = ask("Type the 6-digit code the app shows now: ").strip().replace(" ", "")
        ok, _counter = auth.verify_totp(secret, code, clock())
        if ok:
            print("  2FA ON")
            return secret
        print("  wrong code (is the phone's clock right?), try again")
    raise SetupError("the authenticator code was not confirmed: nothing was changed")


# ---------------------------------------------------------------- the system
def run(argv, check=True):
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as e:
        raise SetupError("cannot run %s: %s" % (argv[0], e.strerror or e))
    out = p.stdout.decode("utf-8", "replace")
    if check and p.returncode != 0:
        raise SetupError("%s failed: %s" % (" ".join(argv[:3]), out.strip()[-400:]))
    return p.returncode, out


def ensure_user(etc=PANEL_ETC):
    """The system user bitpin-panel (no login shell, no home); an existing account that can log in is
    somebody else's and is never adopted. Returns its gid."""
    import grp
    import pwd
    try:
        pw = pwd.getpwnam(PANEL_USER)
    except KeyError:
        pw = None
    if pw is None:
        try:
            grp.getgrnam(PANEL_USER)
        except KeyError:
            run(["groupadd", "--system", PANEL_USER])
        run(["useradd", "--system", "--gid", PANEL_USER, "--home-dir", "/nonexistent", "--no-create-home",
             "--shell", "/usr/sbin/nologin", "--comment", "Bitpin bot management panel", PANEL_USER])
        os.makedirs(etc, exist_ok=True)
        open(os.path.join(etc, USER_MARKER), "a").close()
        print("created the system user %s (no login shell)" % PANEL_USER)
        pw = pwd.getpwnam(PANEL_USER)
    elif not pw.pw_shell.endswith(("/nologin", "/false")):
        raise SetupError("a user named %s already exists and can log in: it is not this kit's account - "
                         "nothing was changed" % PANEL_USER)
    return pw.pw_gid


def ensure_dirs(gid, etc=PANEL_ETC):
    for d in (etc, os.path.join(etc, "tls")):
        os.makedirs(d, exist_ok=True)
        os.chown(d, 0, gid)
        os.chmod(d, 0o750)


def ensure_cert(conf, gid, force=False):
    cert, key = conf["tls_cert"], conf["tls_key"]
    if os.path.exists(cert) and os.path.exists(key) and not force:
        print("TLS certificate: keeping %s" % cert)
    else:
        addrs = server_addresses()
        host = socket.gethostname()
        run(openssl_argv(key, cert, addrs[0] if addrs else host, addrs, host))
        print("TLS certificate: created a self-signed one for %s (valid 825 days)" % ", ".join(addrs + [host]))
    for p in (cert, key):
        os.chown(p, 0, gid)
        os.chmod(p, 0o640)
    with open(cert, "r", encoding="ascii") as f:
        return cert_fingerprint(f.read())


def start_units():
    for u in UNITS:
        if not os.path.exists(os.path.join(UNIT_DIR, u)):
            raise SetupError("%s is not installed: update the bot first (update.sh installs the panel's units)" % u)
    run(["systemctl", "daemon-reload"], check=False)
    run(["systemctl", "enable", "--now", "bitpin-bot-panel-helper.socket"])
    run(["systemctl", "enable", "bitpin-bot-panel.service"])
    run(["systemctl", "restart", "bitpin-bot-panel.service"])


def print_access(conf, fingerprint):
    addrs = server_addresses()
    print()
    print("The panel: %s" % "  or  ".join("https://%s:%d/" % ("[%s]" % a if ":" in a else a, conf["port"])
                                         for a in (addrs or ["<server-ip>"])))
    print("Certificate SHA-256 fingerprint. The browser warns about a self-signed certificate: open the")
    print("certificate's details and continue only if it shows exactly this:")
    print("    %s" % fingerprint)
    print("Firewall: open the port yourself if needed, e.g.  sudo ufw allow %d/tcp  (and in the provider's" % conf["port"])
    print("cloud firewall, if it has one). Stop the panel any time: sudo systemctl disable --now bitpin-bot-panel")


def cmd_setup(args, ask=input, secret_ask=getpass.getpass, clock=time.time):
    from bitpin import panel_auth as auth
    path = conf_path()
    old = read_conf(path)
    gid = ensure_user()
    ensure_dirs(gid)
    username = ask_username(ask, old.get("username"))
    phash = ask_password(secret_ask, username, auth)
    totp = ask_totp(ask, username, auth, clock)
    port = args.port or old.get("port") or DEFAULT_PORT
    conf = build_conf(old, username=username, password_hash=phash, totp_secret=totp, port=port)
    fp = ensure_cert(conf, gid, force=args.new_cert)
    write_conf(conf, path, gid)
    print("wrote %s (root:%s 0640; the password only as a PBKDF2 hash)" % (path, PANEL_USER))
    start_units()
    print("started: bitpin-bot-panel-helper.socket, bitpin-bot-panel.service")
    print_access(conf, fp)
    return 0


def cmd_password(args, ask=input, secret_ask=getpass.getpass):
    from bitpin import panel_auth as auth
    path = conf_path()
    old = read_conf(path)
    if not old:
        raise SetupError("the panel is not set up yet: sudo bitpin-bot panel-setup")
    username = ask_username(ask, old.get("username")) if args.username else old.get("username")
    conf = build_conf(old, username=username, password_hash=ask_password(secret_ask, username, auth))
    write_conf(conf, path, os.stat(path).st_gid)
    run(["systemctl", "try-restart", "bitpin-bot-panel.service"], check=False)
    print("password changed; the panel was restarted (every open session ended)")
    return 0


def cmd_totp(args, ask=input, clock=time.time):
    from bitpin import panel_auth as auth
    path = conf_path()
    old = read_conf(path)
    if not old:
        raise SetupError("the panel is not set up yet: sudo bitpin-bot panel-setup")
    secret = None if args.off else ask_totp(ask, old.get("username") or "owner", auth, clock, confirm_first=False)
    write_conf(build_conf(old, totp_secret=secret), path, os.stat(path).st_gid)
    run(["systemctl", "try-restart", "bitpin-bot-panel.service"], check=False)
    print("2FA %s; the panel was restarted" % ("ON" if secret else "OFF"))
    return 0


def cmd_status(args):
    conf = read_conf(conf_path())
    if not conf:
        print("the panel is not set up: sudo bitpin-bot panel-setup")
        return 1
    for u in UNITS:
        _rc, act = run(["systemctl", "is-active", u], check=False)
        _rc, en = run(["systemctl", "is-enabled", u], check=False)
        print("%-32s %s, %s" % (u, act.strip() or "?", en.strip() or "?"))
    print("username: %s   2FA: %s   port: %s" % (conf.get("username"), "ON" if conf.get("totp_secret") else "OFF",
                                                 conf.get("port")))
    try:
        with open(conf["tls_cert"], "r", encoding="ascii") as f:
            print_access(conf, cert_fingerprint(f.read()))
    except (OSError, KeyError, ValueError) as e:
        print("certificate: not readable (%s)" % e)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bitpin-bot panel-...", description="Set up the management panel.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("setup")
    p.add_argument("--port", type=int, help="the HTTPS port (default 8443)")
    p.add_argument("--new-cert", action="store_true", help="make a new self-signed certificate")
    p = sub.add_parser("password")
    p.add_argument("--username", action="store_true", help="also choose a new username")
    p = sub.add_parser("totp")
    p.add_argument("--off", action="store_true", help="turn the authenticator code off")
    sub.add_parser("status")
    args = ap.parse_args(argv)
    if args.cmd is None:
        ap.print_help()
        return 2
    if getattr(args, "port", None) is not None and not 1024 <= args.port <= 65535:
        print("--port must be 1024..65535 (443 is usually nginx's)", file=sys.stderr)
        return 2
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("run it as root: sudo bitpin-bot panel-%s" % args.cmd, file=sys.stderr)
        return 2
    os.umask(0o077)
    try:
        return {"setup": cmd_setup, "password": cmd_password, "totp": cmd_totp, "status": cmd_status}[args.cmd](args)
    except SetupError as e:
        print("REFUSED: %s" % e, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\ncancelled - nothing more was changed", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
