# -*- coding: utf-8 -*-
"""v3.8.2: a trusted certificate for the management panel's domain (docs/PANEL_FA.md).

Let's Encrypt proves the domain with a DNS TXT record through Cloudflare: the panel listens on its own port (8443),
and ports 80 and 443 usually belong to another web server on the same machine, so the HTTP and TLS-ALPN challenges
are out. certbot's dns-cloudflare plugin writes the TXT record with an API token the owner made for the DNS of one
zone (ACME_CREDENTIALS, root 0600; never printed, never logged). scripts/panel_setup.py is the command line
(sudo bitpin-bot panel-cert DOMAIN, certbot's deploy hook, the renewal service); this module holds the parts that
can be tested without root.

Apart from any other certbot on the server: this ACME client has its own config, work and logs directories
(ACME_CONFIG, ACME_WORK, ACME_LOGS) and its own renewal timer (bitpin-bot-panel-cert.timer); the certbot of the
server's other sites (/etc/letsencrypt, certbot.timer) never sees it. The certificate is copied to the panel's own
paths (panel.json tls_cert / tls_key, root:bitpin-panel 0640) and the first self-signed pair is kept next to them
(*.selfsigned.pem) for panel-cert --off.

IPv4 first: on a server whose IPv6 route to Cloudflare drops packets, every API call waited a minute for the IPv6
attempt to time out. prefer_ipv4() puts the IPv4 answers of getaddrinfo first in the calling process only.
"""
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import tempfile
import time

ACME_CONFIG = "/etc/bitpin-bot-panel/acme"            # root 0700: the ACME account, the certificates, the token
ACME_WORK = "/var/lib/bitpin-bot-panel-acme"
ACME_LOGS = "/var/log/bitpin-bot-panel-acme"
ACME_CREDENTIALS = ACME_CONFIG + "/cloudflare.ini"
CERT_NAME = "bitpin-panel"
RENEW_SERVICE = "bitpin-bot-panel-cert.service"
RENEW_TIMER = "bitpin-bot-panel-cert.timer"
PROPAGATION_SECONDS = 30
SELF_SIGNED_SUFFIX = ".selfsigned.pem"
WARN_DAYS = 14                      # a certificate this close to its end is a warning (it renews 30 days before)
CF_VERIFY_URL = "https://api.cloudflare.com/client/v4/user/tokens/verify"
_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{20,200}$")
_PEM_RE = re.compile(r"-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----")


# --------------------------------------------------------------------------- names and the token
def clean_domain(name):
    """The domain as the certificate gets it: trimmed, lower case, no trailing dot."""
    return str(name or "").strip().lower().rstrip(".")


def domain_problem(name):
    """Why `name` (clean_domain) cannot be the panel's certificate name, or None."""
    if not name:
        return "no domain given"
    try:
        ipaddress.ip_address(name)
        return "an IP address, not a domain name (Let's Encrypt certifies names here)"
    except ValueError:
        pass
    if len(name) > 253:
        return "longer than 253 characters"
    labels = name.split(".")
    if len(labels) < 2:
        return "a full name with a dot is needed (e.g. panel.example.com)"
    if any(not _LABEL_RE.match(x) for x in labels):
        return "only letters a-z, digits and '-' between the dots"
    if labels[-1].isdigit():
        return "the last part cannot be a number"
    return None


def clean_token(text):
    """The pasted token without spaces around it or a copied 'Bearer ' in front."""
    t = str(text or "").strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    return t


def token_problem(token):
    """Why a pasted Cloudflare API token cannot be right, or None (the shape only: verify_token asks Cloudflare)."""
    if not token:
        return "nothing was pasted"
    if not _TOKEN_RE.match(token):
        return "an API token is 20-200 letters, digits and - _ . (no spaces); was the whole token copied?"
    return None


def write_credentials(path, token):
    """The certbot plugin's credentials file: root 0600 in a 0700 directory, written next to its place and moved."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".cloudflare.", dir=d)
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write("# Cloudflare API token of the panel certificate (the DNS of one zone). Root only; made by\n"
                    "# sudo bitpin-bot panel-cert. A new token: sudo bitpin-bot panel-cert DOMAIN --new-token\n"
                    "dns_cloudflare_api_token = %s\n" % token)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def credentials_present(path):
    try:
        with open(path, "r", encoding="ascii", errors="replace") as f:
            return "dns_cloudflare_api_token" in f.read(4096)
    except OSError:
        return False


# --------------------------------------------------------------------------- the network
def prefer_ipv4():
    """getaddrinfo answers IPv4 first in this process (IPv6 stays as the second choice)."""
    orig = socket.getaddrinfo
    if getattr(orig, "_ipv4_first", False):
        return

    def getaddrinfo(*args, **kwargs):
        return sorted(orig(*args, **kwargs), key=lambda r: 0 if r[0] == socket.AF_INET else 1)
    getaddrinfo._ipv4_first = True
    socket.getaddrinfo = getaddrinfo


def verify_token(token, opener=None, timeout=20):
    """(True, "") when Cloudflare says the token is active, else (False, why). The token goes only into the
    Authorization header of this one request to api.cloudflare.com; `why` never contains it."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(CF_VERIFY_URL, headers={"Authorization": "Bearer " + token,
                                                         "User-Agent": "bitpin-bot-panel"})
    opener = opener or urllib.request.urlopen
    try:
        resp = opener(req, timeout=timeout)
        status, body = getattr(resp, "status", 200), resp.read(65536)
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read(65536) if e.fp is not None else b""
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, _scrub("Cloudflare could not be reached: %s" % getattr(e, "reason", e), token)
    try:
        doc = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        doc = {}
    doc = doc if isinstance(doc, dict) else {}
    result = doc.get("result") if isinstance(doc.get("result"), dict) else {}
    if doc.get("success") is True and result.get("status") == "active":
        return True, ""
    errors = ["%s (%s)" % (e.get("message"), e.get("code")) for e in doc.get("errors") or [] if isinstance(e, dict)]
    why = "; ".join(errors) or ("the token is %s" % result["status"] if result.get("status") else "HTTP %s" % status)
    return False, _scrub(why, token)


def _scrub(text, token):
    text = str(text)
    if token:
        text = text.replace(token, "<token>")
    return text[:300]


def resolve_addresses(name, resolve=None):
    """The addresses `name` resolves to (IPv4 first), [] when it does not (yet)."""
    resolve = resolve or socket.getaddrinfo
    try:
        infos = resolve(name, None)
    except (socket.gaierror, OSError, UnicodeError):
        return []
    out = []
    for fam, _t, _p, _c, addr in sorted(infos, key=lambda r: 0 if r[0] == socket.AF_INET else 1):
        a = addr[0]
        if a not in out:
            out.append(a)
    return out


# --------------------------------------------------------------------------- certbot
def live_dir():
    return os.path.join(ACME_CONFIG, "live", CERT_NAME)


def renewal_conf():
    return os.path.join(ACME_CONFIG, "renewal", CERT_NAME + ".conf")


def certbot_args(domain, deploy_hook):
    """certbot certonly for the panel: no e-mail (the owner's choice), the DNS-01 challenge through Cloudflare,
    an ECDSA key, the deploy hook that installs a renewed certificate for the panel. An existing certificate that
    is not due is kept (no new one per run: Let's Encrypt limits how many a name gets)."""
    return ["certonly", "--non-interactive", "--agree-tos", "--register-unsafely-without-email",
            "--config-dir", ACME_CONFIG, "--work-dir", ACME_WORK, "--logs-dir", ACME_LOGS,
            "--authenticator", "dns-cloudflare", "--dns-cloudflare-credentials", ACME_CREDENTIALS,
            "--dns-cloudflare-propagation-seconds", str(PROPAGATION_SECONDS),
            "--cert-name", CERT_NAME, "--key-type", "ecdsa", "--keep-until-expiring",
            "--deploy-hook", deploy_hook, "-d", domain]


def renew_args():
    """certbot renew of this client only (the saved settings of certbot_args: the plugin, the token file, the hook)."""
    return ["renew", "--non-interactive", "--config-dir", ACME_CONFIG, "--work-dir", ACME_WORK, "--logs-dir", ACME_LOGS]


# --------------------------------------------------------------------------- the certificate
def _first_pem(text):
    m = _PEM_RE.search(text or "")
    return m.group(0) if m else None


def cert_info(path, now=None):
    """{names, issuer, self_signed, not_after, days_left, fingerprint} of the first certificate in a PEM file (the
    panel's own one in a chain), else {"error": why}. Never raises."""
    now = time.time() if now is None else now
    try:
        with open(path, "r", encoding="ascii", errors="replace") as f:
            pem = _first_pem(f.read(1024 * 1024))
    except OSError as e:
        return {"error": "cannot read %s: %s" % (path, e.strerror or e)}
    if not pem:
        return {"error": "%s holds no certificate" % path}
    try:
        der = ssl.PEM_cert_to_DER_cert(pem)
    except ValueError as e:
        return {"error": "%s: %s" % (path, e)}
    h = hashlib.sha256(der).hexdigest().upper()
    out = {"fingerprint": ":".join(h[i:i + 2] for i in range(0, len(h), 2))}
    decode = getattr(getattr(ssl, "_ssl", None), "_test_decode_cert", None)
    if decode is None:
        out["error"] = "this Python cannot read the certificate's details"
        return out
    fd, tmp = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(pem + "\n")
        d = decode(tmp)
    except (ssl.SSLError, OSError, ValueError) as e:
        out["error"] = "the certificate cannot be read: %s" % e
        return out
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    subject, issuer = _rdn(d.get("subject")), _rdn(d.get("issuer"))
    names = ["%s" % v for k, v in d.get("subjectAltName") or () if k in ("DNS", "IP Address")]
    if not names and subject.get("commonName"):
        names = [subject["commonName"]]
    out["names"] = names
    out["self_signed"] = bool(subject) and subject == issuer
    org, cn = issuer.get("organizationName"), issuer.get("commonName")
    out["issuer"] = ("%s (%s)" % (org, cn) if org and cn else org or cn or "?")
    try:
        out["not_after"] = float(ssl.cert_time_to_seconds(d["notAfter"]))
        out["days_left"] = int((out["not_after"] - now) // 86400)
    except (KeyError, ValueError, OverflowError):
        out["not_after"] = out["days_left"] = None
    return out


def _rdn(seq):
    out = {}
    for rdn in seq or ():
        for k, v in rdn:
            out.setdefault(k, v)
    return out


def summary(info, renewal=None):
    """One line for 'bitpin-bot health' and the setup's output; renewal: 'on' / 'off' / None (unknown)."""
    if not isinstance(info, dict) or "names" not in info:
        return "unreadable (%s)" % ((info or {}).get("error") or "?")
    kind = "self-signed" if info.get("self_signed") else info.get("issuer") or "?"
    text = "%s for %s" % (kind, ", ".join(info["names"]) or "?")
    if info.get("not_after") is not None:
        text += ", valid until %s (%d days left)" % (time.strftime("%Y-%m-%d", time.gmtime(info["not_after"])),
                                                   info["days_left"])
    if renewal:
        text += ", automatic renewal %s" % renewal
    days = info.get("days_left")
    if days is not None and days < 0:
        text = "EXPIRED: " + text
    elif days is not None and days < WARN_DAYS and not info.get("self_signed"):
        text = "WARN: " + text
    return text


def check_pair(cert, key):
    """Raises ssl.SSLError / OSError unless `cert` and `key` load as one TLS server pair."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)


def backup_path(path):
    base = path[:-4] if path.endswith(".pem") else path
    return base + SELF_SIGNED_SUFFIX


def _copy_into_place(src, dst, gid):
    d = os.path.dirname(os.path.abspath(dst))
    fd, tmp = tempfile.mkstemp(prefix=".panel-tls.", dir=d)
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        os.chmod(tmp, 0o640)
        if gid is not None and hasattr(os, "chown") and os.geteuid() == 0:     # root in production, not in the tests
            os.chown(tmp, 0, gid)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def install_cert(lineage, cert_path, key_path, gid=None):
    """<lineage>/fullchain.pem and privkey.pem become the panel's certificate and key: checked first (they must
    load as one pair), the panel's first self-signed pair kept as *.selfsigned.pem (only a self-signed one, and
    only once), each file written next to its place and moved there, root:<gid> 0640 like panel-setup's own.
    Returns cert_info() of the installed certificate."""
    chain, key = os.path.join(lineage, "fullchain.pem"), os.path.join(lineage, "privkey.pem")
    check_pair(chain, key)
    if os.path.isfile(cert_path) and os.path.isfile(key_path) and cert_info(cert_path).get("self_signed"):
        for p in (cert_path, key_path):
            if not os.path.exists(backup_path(p)):
                _copy_into_place(p, backup_path(p), gid)
    _copy_into_place(key, key_path, gid)
    _copy_into_place(chain, cert_path, gid)
    return cert_info(cert_path)


def restore_selfsigned(cert_path, key_path, gid=None):
    """The kept self-signed pair back in place (True), or False when there is none."""
    bc, bk = backup_path(cert_path), backup_path(key_path)
    if not (os.path.isfile(bc) and os.path.isfile(bk)):
        return False
    check_pair(bc, bk)
    _copy_into_place(bk, key_path, gid)
    _copy_into_place(bc, cert_path, gid)
    return True
