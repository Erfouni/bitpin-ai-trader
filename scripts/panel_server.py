#!/usr/bin/env python3
"""HTTPS server of the management panel (v3.1): serves bitpin/panel_web.py's PanelApp.

  python3 scripts/panel_server.py [--config /etc/bitpin-bot-panel/panel.json] [--check]

Runs as the unprivileged system user bitpin-panel (systemd: NoNewPrivileges, ProtectSystem=strict, no capabilities,
writable /var/lib/bitpin-bot-panel only). It never sees an API key: every privileged action is one JSON line to the
root helper (scripts/panel_helper.py) on its UNIX socket (helper_socket). --check validates the config and the
certificate and exits.

/etc/bitpin-bot-panel/panel.json (directory root:bitpin-panel 0750, file 0640): bind, port,
tls_cert (/etc/bitpin-bot-panel/tls/cert.pem), tls_key (/etc/bitpin-bot-panel/tls/key.pem), username, password_hash
("pbkdf2_sha256$600000$<salt b64>$<hash b64>", bitpin.panel_auth.hash_password), totp_secret (base32 or null),
allowed_hosts ([] = any Host header), session_idle_minutes, session_max_hours, helper_socket, audit_log,
trusted_proxy (true only behind a local reverse proxy: the client IP is then the last X-Forwarded-For entry).

Hardening here: TLS 1.2+ only (ECDHE AEAD suites for 1.2), the TLS handshake runs in the connection's thread
(a stalled client cannot block accept()), at most MAX_CONNECTIONS connections at once, a per-read timeout plus
a total deadline for the handshake + request, request bodies up to 5 MB with a Content-Length only, one request
per connection, no Server version. The log (stderr -> journal) has the client IP, method, path without the query
string and status - never a header, cookie, form field or body.

Exit codes: 0 stopped, 1 cannot listen, 78 configuration / certificate problem (systemd should not restart).
SIGHUP (systemctl reload, v3.8.2): the certificate files are loaded again for the next connections (a renewed
certificate of panel-cert); the sessions stay.
"""
import argparse
import ipaddress
import json
import logging
import os
import signal
import socket
import socketserver
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from bitpin.panel_web import HelperClient, PanelApp, config_problems, load_static  # noqa: E402

log = logging.getLogger("bitpin.panel")

DEFAULT_CONFIG = "/etc/bitpin-bot-panel/panel.json"
DEFAULT_SOCKET = "/run/bitpin-panel/helper.sock"
MAX_BODY = 5 * 1024 * 1024
READ_TIMEOUT = 20.0              # seconds per socket read / write while the request arrives
REQUEST_DEADLINE = 60.0          # the TLS handshake + request line + headers + body, in total
MAX_CONNECTIONS = 64
EXIT_CONFIG = 78


class ConfigError(Exception):
    pass


def load_config(path):
    """The parsed panel.json; ConfigError with a clear message when it cannot be used."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        raise ConfigError("the panel config %s does not exist (create it: 0640 root:bitpin-panel)" % path)
    except PermissionError:
        raise ConfigError("cannot read %s: permission denied (the directory must be 0750 and the file 0640, both "
                          "root:bitpin-panel)" % path)
    except OSError as e:
        raise ConfigError("cannot read %s: %s" % (path, e.strerror or e))
    except ValueError as e:
        raise ConfigError("%s is not valid JSON: %s" % (path, e))
    problems = config_problems(cfg)
    if isinstance(cfg, dict):
        port = cfg.get("port", 8443)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            problems.append("port must be a number 1..65535")
        if not isinstance(cfg.get("bind", "0.0.0.0"), str):
            problems.append("bind must be an address string")
        for key in ("tls_cert", "tls_key"):
            p = cfg.get(key)
            if not isinstance(p, str) or not p:
                problems.append("%s is not set" % key)
            elif not os.path.isfile(p):
                problems.append("%s: the file %s does not exist" % (key, p))
            elif not os.access(p, os.R_OK):
                problems.append("%s: %s is not readable by this user" % (key, p))
    if problems:
        raise ConfigError("%s: %s" % (path, "; ".join(problems)))
    return cfg


def make_ssl_context(cert, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.options |= ssl.OP_NO_COMPRESSION
    ctx.options |= getattr(ssl, "OP_CIPHER_SERVER_PREFERENCE", 0) | getattr(ssl, "OP_NO_RENEGOTIATION", 0)
    ctx.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20")     # TLS 1.2 suites; the TLS 1.3 suites are all AEAD
    ctx.load_cert_chain(cert, key)
    return ctx


def client_ip(peer_ip, forwarded_for, trusted_proxy):
    """The client address: the socket's peer, or behind a trusted local proxy the last X-Forwarded-For entry
    (the one the proxy itself appended; earlier entries are whatever the client claimed)."""
    if trusted_proxy and forwarded_for:
        last = str(forwarded_for).split(",")[-1].strip()
        try:
            return str(ipaddress.ip_address(last))
        except ValueError:
            pass
    return str(peer_ip)


def body_length(method, headers):
    """(length, None) or (None, (status, reason)) from the request headers (an HTTPMessage or a dict)."""
    get_all = getattr(headers, "get_all", None)
    if headers.get("Transfer-Encoding"):
        return None, (501, "Transfer-Encoding is not supported")
    values = get_all("Content-Length") if get_all else ([headers["Content-Length"]] if "Content-Length" in headers else None)
    if not values:
        return (None, (411, "Length Required")) if method == "POST" else (0, None)
    if len(set(v.strip() for v in values)) != 1:
        return None, (400, "conflicting Content-Length")
    text = values[0].strip()
    if not text or len(text) > 12 or any(c not in "0123456789" for c in text):
        return None, (400, "bad Content-Length")
    n = int(text)
    if n > MAX_BODY:
        return None, (413, "Payload Too Large")
    return n, None


def _shutdown_quietly(holder):
    try:
        socket.socket.shutdown(holder[0], socket.SHUT_RDWR)   # the plain TCP shutdown, also on an SSLSocket
    except (OSError, ValueError, TypeError):
        pass


class PanelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"           # one request per connection
    timeout = READ_TIMEOUT
    server_version = "bitpin-panel"
    sys_version = ""

    def version_string(self):
        return "bitpin-panel"

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    def _dispatch(self, method):
        app = self.server.app
        path = self.path or ""
        if not path.startswith("/") or path.startswith("//"):
            self._send(*app.error_response(400, "Bad Request (path)"))
            return
        n, problem = body_length(method, self.headers)
        if problem is not None:
            self._send(*app.error_response(*problem))
            return
        body = b""
        if method == "POST" and n:
            body = self.rfile.read(n)
            if len(body) != n:
                self.close_connection = True
                return
        self.server.request_read()
        path, _, query = path.partition("?")
        ip = client_ip(self.client_address[0], self.headers.get("X-Forwarded-For"), self.server.trusted_proxy)
        status, headers, out = app.handle(method, path, query, self.headers, body, ip)
        self._send(status, headers, out, head=(method == "HEAD"))

    def _send(self, status, headers, body, head=False):
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body and not head:
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        """The base class's own errors (a bad request line, headers too long ...) with the security headers."""
        self.close_connection = True
        reason = message or self.responses.get(code, ("Error",))[0]
        try:
            self._send(*self.server.app.error_response(code, reason), head=(getattr(self, "command", "") == "HEAD"))
        except OSError:
            pass

    def log_request(self, code="-", size="-"):
        path = str(getattr(self, "path", "") or "").split("?", 1)[0][:200]
        log.info('%s "%s %s" %s', self.client_address[0], getattr(self, "command", None) or "-", path,
                 getattr(code, "value", code))

    def log_message(self, format, *args):
        log.info("%s %s", self.client_address[0], (format % args)[:300])

    def log_error(self, format, *args):
        log.warning("%s %s", self.client_address[0], (format % args)[:300])


PANEL_MSS = 1200          # bytes of payload per TCP segment (IP packets of about 1240 bytes)


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, address, app, ssl_context, trusted_proxy=False, max_connections=MAX_CONNECTIONS,
                 handler=PanelHandler):
        self.app = app
        self.ssl_context = ssl_context
        self.trusted_proxy = bool(trusted_proxy)
        self._slots = threading.BoundedSemaphore(max_connections)
        self._local = threading.local()
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        ThreadingHTTPServer.__init__(self, address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            log.warning("%s dropped: %d connections are open already", client_address[0], MAX_CONNECTIONS)
            self.shutdown_request(request)
            return
        try:
            ThreadingHTTPServer.process_request(self, request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            ThreadingHTTPServer.process_request_thread(self, request, client_address)
        finally:
            self._slots.release()

    def finish_request(self, request, client_address):
        holder = [request]
        timer = threading.Timer(REQUEST_DEADLINE, _shutdown_quietly, (holder,))
        timer.daemon = True
        self._local.deadline = timer
        timer.start()
        try:
            request.settimeout(READ_TIMEOUT)
            try:
                tls = self.ssl_context.wrap_socket(request, server_side=True)
            except (ssl.SSLError, OSError, ValueError):
                return                                     # scanners, plain HTTP, slow handshakes: dropped
            holder[0] = tls
            try:
                self.RequestHandlerClass(tls, client_address, self)
            finally:
                try:
                    tls.close()
                except OSError:
                    pass
        finally:
            timer.cancel()

    def server_bind(self):
        """HTTPServer.server_bind without its reverse-DNS lookup (socket.getfqdn): a broken resolver must not
        delay the start. 3.1.1: small TCP segments (TCP_MAXSEG, inherited by every accepted connection): a VPN
        path that silently drops full-size packets delivered the small login page but never the 5 KB stylesheet,
        so the page stayed black. A panel page is a few KB: the smaller segments cost nothing."""
        if hasattr(socket, "TCP_MAXSEG"):
            try:
                self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, PANEL_MSS)
            except OSError as e:
                log.warning("panel: cannot set TCP_MAXSEG %d (%s): full-size segments", PANEL_MSS, e)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = str(self.server_address[0]), self.server_address[1]

    def request_read(self):
        """The whole request has arrived: the deadline stops (a helper call may take up to 300 s)."""
        timer = getattr(self._local, "deadline", None)
        if timer is not None:
            timer.cancel()

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        log.warning("%s connection error: %s", client_address[0] if client_address else "?",
                    exc.__class__.__name__ if exc else "?")


def make_server(cfg, app, ssl_context):
    bind = cfg.get("bind") or "0.0.0.0"
    return PanelServer((bind, int(cfg.get("port") or 8443)), app, ssl_context, bool(cfg.get("trusted_proxy")))


def reload_certificate(server, cfg):
    """v3.8.2 (SIGHUP): a new TLS context from the certificate files for the next connections - open sessions and
    connections stay. A pair that cannot be loaded leaves the old one in use. True when it was replaced."""
    try:
        ctx = make_ssl_context(cfg["tls_cert"], cfg["tls_key"])
    except (ssl.SSLError, OSError, ValueError, KeyError) as e:
        log.error("panel: the TLS certificate could not be reloaded (%s): the old one stays", e)
        return False
    server.ssl_context = ctx
    log.info("panel: TLS certificate reloaded")
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="HTTPS server of the bitpin-bot management panel")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="panel.json (default %(default)s)")
    ap.add_argument("--check", action="store_true", help="validate the config and the certificate, then exit")
    args = ap.parse_args(argv)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    cfg = {}
    try:
        cfg = load_config(args.config)
        ctx = make_ssl_context(cfg["tls_cert"], cfg["tls_key"])
    except ConfigError as e:
        sys.stderr.write("panel: %s\n" % e)
        return EXIT_CONFIG
    except (ssl.SSLError, OSError, ValueError) as e:
        sys.stderr.write("panel: cannot load the TLS certificate / key (%s, %s): %s\n"
                         % (cfg.get("tls_cert"), cfg.get("tls_key"), e))
        return EXIT_CONFIG
    if args.check:
        sys.stdout.write("panel: config and certificate OK\n")
        if load_static("panel.css")[1] == "missing":
            sys.stdout.write("panel: WARNING bitpin/static/panel.css is missing: the pages would have no styles\n")
        return 0
    app = PanelApp(cfg, HelperClient(cfg.get("helper_socket") or DEFAULT_SOCKET), config_path=args.config)
    try:
        server = make_server(cfg, app, ctx)
    except OSError as e:
        sys.stderr.write("panel: cannot listen on %s:%s: %s\n" % (cfg.get("bind"), cfg.get("port"), e.strerror or e))
        return 1
    if cfg.get("trusted_proxy") and cfg.get("bind") not in ("127.0.0.1", "::1", "localhost"):
        log.warning("trusted_proxy is on but bind is %s: a client that reaches the port directly can fake its "
                    "IP with X-Forwarded-For (bind 127.0.0.1 behind the proxy)", cfg.get("bind"))

    def _stop(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, _stop)
    if hasattr(signal, "SIGHUP"):                # v3.8.2: systemctl reload - the renewed certificate
        signal.signal(signal.SIGHUP, lambda signum, frame: reload_certificate(server, cfg))
    log.info("panel listening on https://%s:%s (2FA %s, allowed hosts: %s, trusted proxy: %s)",
             cfg.get("bind") or "0.0.0.0", cfg.get("port") or 8443, "on" if cfg.get("totp_secret") else "OFF",
             ", ".join(app.allowed_hosts) or "any", "yes" if cfg.get("trusted_proxy") else "no")
    if not cfg.get("totp_secret"):
        log.warning("two-factor login is OFF: turn it on in the panel (Security page)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("panel stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
