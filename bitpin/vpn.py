"""xray (V2Ray) client configuration for the owner's web management panel: import a share link, swap
the proxy server in the tunnel's config, show a masked summary, validate the file with the xray binary
and probe the local proxy (stdlib only, Python 3.7+; imports nothing from bitpin).

WHY: the bot reaches Moonshot (Kimi), OpenRouter and Telegram through a local xray tunnel
(`xray-tunnel.service`: `/opt/xray/xray run -c /opt/xray/config.json` as user nobody, an HTTP proxy
inbound on 127.0.0.1:1081). When the provider hands out a new server the owner gets a share link; the
panel (through its privileged helper) turns it into the config with these functions instead of a
hand-edited JSON file:

    config = loads_config(text)                      # JSON, // and /* */ comments tolerated
    outbound, meta = parse_share_link(link)          # ValueError with a clear English message
    new_config, tag = replace_proxy_outbound(config, outbound)
    new_text = dumps_config(new_config)
    ok, output = validate_with_xray(new_text, ["/opt/xray/xray"])
    if ok:
        atomic_write_text("/opt/xray/config.json", new_text)   # then restart xray-tunnel.service
    proxy_test("http://127.0.0.1:1081")              # CONNECT + TLS handshake to the bot's hosts

apply_share_link(text, link) does the first four steps in one call.

* Share links: vmess:// (the V2RayN base64 JSON, and the vmess://uuid@host:port?... URL form), vless://,
  trojan:// and ss:// (SIP002 with a base64 or a percent-encoded method:password, and the legacy
  ss://base64(method:password@host:port) form). Transports tcp (optionally with the HTTP header), ws,
  grpc, http/h2, httpupgrade, xhttp/splithttp; security none, tls and reality. IPv6 hosts in brackets.
  The result is a standard xray outbound tagged "proxy" plus a small `meta` dict for display.
* Position matters: xray sends every connection no routing rule claims to the FIRST outbound, so the new
  proxy outbound takes the replaced outbound's index and tag (routing rules keep pointing at it).
* Secrets (user ids, passwords, keys, seeds) never appear in an exception message: link errors name the
  problem, never the value, and are raised with no chained exception that could carry the decoded link.
  summarize_outbound() and mask_config() show a secret as its first 4 characters + "..." (one ellipsis
  character) + its last 2, or "****" when it is shorter than 16 characters; validate_with_xray() redacts
  the config's secrets from xray's output as well.
* No side effect at import time; the only network I/O is proxy_test(), towards the proxy it is given.
"""
import base64
import binascii
import copy
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import stat
import struct
import subprocess
import tempfile
import time
import urllib.parse

__all__ = ["parse_share_link", "replace_proxy_outbound", "apply_share_link", "summarize_outbound",
           "summarize_config", "mask_config", "mask_secret", "loads_config", "dumps_config",
           "validate_with_xray", "proxy_test", "parse_proxy_url", "atomic_write_text"]

PROXY_TAG = "proxy"
# outbound protocols that carry traffic to a remote server (freedom, blackhole, dns, loopback do not)
PROXY_PROTOCOLS = ("vmess", "vless", "trojan", "shadowsocks", "socks", "http", "wireguard")
MAX_LINK_CHARS = 16384                   # a share link is a few hundred characters; this bounds the work
NAME_MAX_CHARS = 128

# share-link transport name -> xray streamSettings.network ("raw" is the newer name of tcp; "tcp" works
# on every xray version, so it is written as "tcp")
NETWORKS = {"tcp": "tcp", "raw": "tcp", "ws": "ws", "websocket": "ws", "grpc": "grpc", "gun": "grpc",
            "http": "http", "h2": "h2", "httpupgrade": "httpupgrade", "xhttp": "xhttp",
            "splithttp": "splithttp"}
SUPPORTED_NETWORKS = "tcp, ws, grpc, http/h2, httpupgrade, xhttp, splithttp"
SECURITIES = ("none", "tls", "reality")
# shadowsocks ciphers xray implements (AEAD, 2022 edition, and the unencrypted "none")
SS_METHODS = ("aes-128-gcm", "aes-256-gcm", "chacha20-poly1305", "chacha20-ietf-poly1305",
              "xchacha20-poly1305", "xchacha20-ietf-poly1305", "2022-blake3-aes-128-gcm",
              "2022-blake3-aes-256-gcm", "2022-blake3-chacha20-poly1305", "none", "plain")
# well-known ciphers xray does NOT implement: named in the error (a cipher name is not a secret)
SS_UNSUPPORTED_METHODS = ("aes-128-cfb", "aes-192-cfb", "aes-256-cfb", "aes-128-cfb8", "aes-192-cfb8",
                          "aes-256-cfb8", "aes-128-ctr", "aes-192-ctr", "aes-256-ctr", "aes-128-ofb",
                          "aes-192-ofb", "aes-256-ofb", "aes-192-gcm", "bf-cfb", "camellia-128-cfb",
                          "camellia-192-cfb", "camellia-256-cfb", "cast5-cfb", "chacha20", "chacha20-ietf",
                          "des-cfb", "idea-cfb", "rc2-cfb", "rc4", "rc4-md5", "rc4-md5-6", "salsa20",
                          "seed-cfb", "table", "xchacha20", "2022-blake3-chacha8-poly1305")
_SS_METHODS_SHOWN = "aes-128-gcm, aes-256-gcm, chacha20-poly1305, xchacha20-poly1305, 2022-blake3-*"

# config keys whose values are secrets (compared case-insensitively). publicKey, shortId, mldsa65Verify
# and a client's "encryption" are public. A VLESS inbound's "decryption" is secret unless it is "none".
SECRET_KEYS = frozenset(k.lower() for k in ("id", "password", "pass", "privateKey", "psk", "preSharedKey",
                                            "secretKey", "seed", "mldsa65Seed", "key", "echServerKeys",
                                            "Authorization", "Proxy-Authorization"))
MASK = "****"
ELLIPSIS = "\u2026"
MASK_PARTIAL_MIN_CHARS = 16              # shorter secrets are masked completely
REDACT_MIN_CHARS = 6                     # shorter "secrets" are not searched for in xray's output

XRAY_OUTPUT_LIMIT = 4000
XRAY_TIMEOUT_S = 20
# markers of an xray / v2ray build that predates the `run` subcommand (xray >= 1.8 syntax)
_UNKNOWN_COMMAND_RE = re.compile(r"unknown (?:sub)?command|flag provided but not defined|no such command|"
                                 r"unrecognized command", re.I)
# environment variables that make xray load more config files than the one given with -c
_XRAY_CONFIG_ENV = ("XRAY_LOCATION_CONFDIR", "XRAY_LOCATION_CONFIG", "xray.location.confdir",
                    "xray.location.config")

DEFAULT_TEST_TARGETS = (("api.moonshot.ai", 443), ("api.telegram.org", 443), ("openrouter.ai", 443))
PROXY_TEST_TIMEOUT_S = 10
_PROXY_REPLY_MAX = 16384
_SOCKS5_REPLIES = {1: "general SOCKS server failure", 2: "connection not allowed by the proxy's rules",
                   3: "network unreachable", 4: "host unreachable", 5: "connection refused by the target",
                   6: "TTL expired", 7: "command not supported", 8: "address type not supported"}

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]{0,31})://")
# a transport / security / plugin name worth echoing in an error: at most 16 characters, a letter, no dot
# (never a 32-hex id, a host name or a sentence)
_TOKEN_RE = re.compile(r"^(?=.*[A-Za-z])[A-Za-z0-9][A-Za-z0-9_+-]{0,15}$")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9_](?:[A-Za-z0-9_.-]{0,251}[A-Za-z0-9_.])?$")
_DIGITS_DOTS_RE = re.compile(r"^[0-9.]+$")
_PORT_RE = re.compile(r"^[0-9]{1,5}$")
_B64_RE = re.compile(r"^[A-Za-z0-9+/]*$")
_SPACE_RE = re.compile(r"\s")
_CTRL_RE = re.compile("[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_INVISIBLE_EDGES = "\ufeff\u200b\u200e\u200f\u2060"


# --------------------------------------------------------------------------- small helpers

def _named(value):
    """" 'value'" for an error message when `value` is a short plain token (a transport, a security
    or a cipher name), else "": a value that could be (part of) a secret is never echoed."""
    v = value if isinstance(value, str) else ""
    return " '%s'" % v if _TOKEN_RE.match(v) else ""


def _text(value):
    """A config value as a display string: "" for None, containers and booleans."""
    if value is None or isinstance(value, (bool, dict, list, tuple)):
        return ""
    return value if isinstance(value, str) else str(value)


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _split_list(value):
    """"a, b" or ["a", "b"] -> ["a", "b"] (empty items dropped)."""
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = [x for x in value if isinstance(x, str)]
    else:
        items = []
    return [x.strip() for x in items if x.strip()]


def _truthy(value):
    if value is True:
        return True
    if isinstance(value, int) and not isinstance(value, bool):
        return value == 1
    return isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on")


def _utf8(data):
    """bytes -> str, or None when they are not UTF-8 (a leading BOM is dropped)."""
    if not isinstance(data, (bytes, bytearray)):
        return None
    try:
        return bytes(data).decode("utf-8").lstrip("\ufeff")
    except UnicodeDecodeError:
        return None


def _b64decode(text):
    """Tolerant base64: standard or URL-safe alphabet, missing or extra padding, whitespace (a wrapped
    link). Returns bytes, or None when the text is not base64."""
    if not isinstance(text, str):
        return None
    s = re.sub(r"\s+", "", text).replace("-", "+").replace("_", "/").rstrip("=")
    if not s or not _B64_RE.match(s) or len(s) % 4 == 1:
        return None
    try:
        return base64.b64decode(s + "=" * (-len(s) % 4), validate=True)
    except (binascii.Error, ValueError):
        return None


def _json_object(text):
    """JSON text -> dict, or None (not JSON, not an object, nested too deep)."""
    if not isinstance(text, str):
        return None
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def _is_ip(text, version):
    try:
        return ipaddress.ip_address(text).version == version
    except ValueError:
        return False


def _idna(host):
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def _clean_name(text):
    """A share link's display name: control and bidi-override characters removed, whitespace collapsed,
    at most NAME_MAX_CHARS characters."""
    return " ".join(_CTRL_RE.sub(" ", text or "").split())[:NAME_MAX_CHARS]


def _strip_edges(text):
    """Whitespace and invisible characters a copy from a chat app leaves around a link."""
    previous = None
    while previous != text:
        previous = text
        text = text.strip().strip(_INVISIBLE_EDGES)
    return text


def _port(value, what):
    """A port as int 1..65535 (int, integral float or digit string), else ValueError (the value is
    not echoed)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError("the %s port is missing" % what)
    n = None
    if isinstance(value, bool):
        n = None
    elif isinstance(value, int):
        n = value
    elif isinstance(value, float):
        n = int(value) if math.isfinite(value) and value.is_integer() else None
    elif isinstance(value, str) and _PORT_RE.match(value.strip()):
        n = int(value.strip())
    if n is None or not 1 <= n <= 65535:
        raise ValueError("the %s port is not a number between 1 and 65535" % what)
    return n


def _nonneg_int(value):
    """vmess alterId: None / "" -> 0, a whole number >= 0 -> int, anything else -> None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return 0
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() and value >= 0 else None
    if isinstance(value, str) and re.match(r"^[0-9]{1,5}$", value.strip()):
        return int(value.strip())
    return None


def _hostname(host, what):
    """A server address: a DNS name (an internationalised one is converted to its ASCII form), an IPv4
    address, or an IPv6 address with or without brackets (returned without them)."""
    h = (host or "").strip()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
        if not _is_ip(h, 6):
            raise ValueError("the %s address in brackets is not an IPv6 address" % what)
        return h
    if not h:
        raise ValueError("the %s address is missing" % what)
    if ":" in h:
        if _is_ip(h, 6):
            return h
        raise ValueError("the %s address is not a valid host name or IP address" % what)
    if not h.isascii():
        h = _idna(h)
        if h is None:
            raise ValueError("the %s address is not a valid host name" % what)
    if not _HOSTNAME_RE.match(h) or (_DIGITS_DOTS_RE.match(h) and not _is_ip(h, 4)):
        raise ValueError("the %s address is not a valid host name or IP address" % what)
    return h


def _host_port(text, what):
    """"host:port" or "[v6]:port" -> (host, port); ValueError names the problem, never the value."""
    hp = (text or "").strip()
    if not hp:
        raise ValueError("the %s address is missing" % what)
    if hp.startswith("["):
        end = hp.find("]")
        if end < 0:
            raise ValueError("the %s address has an unclosed '[' (an IPv6 address is written like "
                             "[2001:db8::1]:443)" % what)
        host, tail = hp[1:end], hp[end + 1:]
        if not _is_ip(host, 6):
            raise ValueError("the %s address in brackets is not an IPv6 address" % what)
        if not tail:
            raise ValueError("the %s port is missing" % what)
        if not tail.startswith(":"):
            raise ValueError("the %s address has text after ']' that is not ':port'" % what)
        return host, _port(tail[1:], what)
    host, colon, port = hp.rpartition(":")
    if not colon:
        raise ValueError("the %s port is missing (expected host:port)" % what)
    if ":" in host:
        raise ValueError("write the IPv6 %s address in brackets, like [2001:db8::1]:443" % what)
    return _hostname(host, what), _port(port, what)


def _format_host_port(host, port):
    return "[%s]:%d" % (host, port) if ":" in host else "%s:%d" % (host, port)


def _query(query):
    """"a=1&B=%2F" -> {"a": "1", "b": "/"}: keys lower-cased (links spell serviceName, headerType,
    allowInsecure both ways), values percent-decoded without turning "+" into a space (base64 keys and
    paths keep their "+"), the first occurrence of a key wins."""
    out = {}
    for part in (query or "").split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        key = urllib.parse.unquote(key).strip().lower()
        if key and key not in out:
            out[key] = urllib.parse.unquote(value).strip()
    return out


# --------------------------------------------------------------------------- share links

def parse_share_link(link):
    """A share link -> (outbound, meta).

    outbound is a standard xray outbound {"tag": "proxy", "protocol": "vmess" | "vless" | "trojan" |
    "shadowsocks", "settings": {...}, "streamSettings": {...}}: vnext/users for vmess (id, alterId,
    security) and vless (id, "encryption": "none" unless the link says otherwise, flow when given),
    servers for trojan (password) and shadowsocks (method, password). meta is {"name", "protocol",
    "address", "port", "network", "security", "sni"} for display (sni: the TLS / REALITY server name,
    "" without either).

    Raises ValueError with a clear English message for a malformed or unsupported link (no scheme, an
    unknown scheme, bad base64, a missing or out-of-range port, an unsupported transport, security or
    cipher, REALITY without a public key, more than one link). The message never contains the user id,
    the password or any other part of the link that could be secret, and the exception carries no
    chained exception.
    """
    if isinstance(link, (bytes, bytearray)):
        link = _utf8(link)
        if link is None:
            raise ValueError("the share link is not UTF-8 text")
    if not isinstance(link, str):
        raise ValueError("the share link must be text")
    text = _strip_edges(link)
    if not text:
        raise ValueError("the share link is empty")
    if len(text) > MAX_LINK_CHARS:
        raise ValueError("the share link is too long (more than %d characters)" % MAX_LINK_CHARS)
    count = sum(1 for token in text.split() if _SCHEME_RE.match(token))
    if count > 1:
        raise ValueError("the text holds %d share links; import one link at a time" % count)
    m = _SCHEME_RE.match(text)
    if not m:
        raise ValueError("not a share link: it must start with vmess://, vless://, trojan:// or ss://")
    scheme = m.group(1).lower()
    body = text[m.end():]
    if scheme == "vmess":
        return _parse_vmess(body)
    if scheme in ("vless", "trojan"):
        return _parse_url_link(scheme, body)
    if scheme == "ss":
        return _parse_ss(body)
    raise ValueError("unsupported share link type '%s' (supported: vmess, vless, trojan, ss)" % scheme)


def _parse_vmess(body):
    """vmess://base64(JSON) as V2RayN exports it: v, ps (name), add, port, id, aid, scy, net, type (the
    TCP header type; the mode for grpc and xhttp), host (the grpc authority), path (the grpc service
    name), tls, sni, alpn, fp. Numbers may be JSON numbers or strings."""
    encoded, _, fragment = body.partition("#")
    encoded = encoded.partition("?")[0]
    if "@" in encoded:                          # base64 has no "@": the vmess://uuid@host:port URL form
        return _parse_url_link("vmess", body)
    data = _b64decode(urllib.parse.unquote(encoded))
    if data is None:
        raise ValueError("vmess link: the text after vmess:// is not valid base64")
    obj = _json_object(_utf8(data))
    if obj is None:
        raise ValueError("vmess link: the base64 text does not decode to a JSON object")

    def field(key):
        return _text(obj.get(key)).strip()

    address = _hostname(field("add"), "vmess server")
    port = _port(obj.get("port"), "vmess server")
    uid = field("id")
    if not uid or _CTRL_RE.search(uid):
        raise ValueError("vmess link: the user id (id) is missing or has control characters")
    alter_id = _nonneg_int(obj.get("aid"))
    if alter_id is None:
        raise ValueError("vmess link: alterId (aid) is not a whole number")
    network = _network(field("net"), "vmess")
    kind = field("type").lower()
    params = {"host": field("host"), "path": field("path"), "sni": field("sni"), "alpn": field("alpn"),
              "fp": field("fp"), "pbk": field("pbk"), "sid": field("sid"), "spx": field("spx"),
              "allowinsecure": obj.get("allowInsecure", obj.get("insecure"))}
    if network == "tcp":
        params["headertype"] = kind
    elif network in ("grpc", "xhttp", "splithttp"):
        params["mode"] = "" if kind == "none" else kind
    tls = obj.get("tls")
    security = _security("tls" if tls is True else _text(tls), "vmess", "none")
    stream, sni = _build_stream("vmess", network, security, params, address)
    user = {"id": uid, "alterId": alter_id, "security": (field("scy") or "auto").lower()}
    outbound = _outbound("vmess", {"vnext": [{"address": address, "port": port, "users": [user]}]}, stream)
    name = field("ps") or urllib.parse.unquote(fragment)
    return outbound, _meta(name, "vmess", address, port, network, security, sni)


def _parse_url_link(protocol, body):
    """vless://id@host:port?params#name, trojan://password@host:port?params#name and the
    vmess://id@host:port?params#name URL form."""
    rest, _, fragment = body.partition("#")
    if _SPACE_RE.search(rest):
        raise ValueError("%s link: the link contains spaces or line breaks" % protocol)
    rest, _, query = rest.partition("?")
    userinfo, at, hostpath = rest.rpartition("@")       # the LAST "@": a raw "@" in a password survives
    what = "password" if protocol == "trojan" else "user id"
    if not at or not userinfo:
        raise ValueError("%s link: the %s before '@' is missing" % (protocol, what))
    secret = urllib.parse.unquote(userinfo)
    if not secret.strip() or _CTRL_RE.search(secret):
        raise ValueError("%s link: the %s before '@' is empty or has control characters" % (protocol, what))
    address, port = _host_port(hostpath.split("/", 1)[0], protocol + " server")
    p = _query(query)
    network = _network(p.get("type"), protocol)
    if protocol == "vless":
        encryption = p.get("encryption") or "none"
        # "none" in any case is "none"; a VLESS-encryption string keeps its case (it embeds a base64 key)
        user = {"id": secret, "encryption": "none" if encryption.lower() == "none" else encryption}
        if p.get("flow"):
            user["flow"] = p["flow"].lower()
        settings = {"vnext": [{"address": address, "port": port, "users": [user]}]}
        security = _security(p.get("security"), protocol, "none")
    elif protocol == "vmess":
        alter_id = _nonneg_int(p.get("aid", p.get("alterid")))
        if alter_id is None:
            raise ValueError("vmess link: alterId (aid) is not a whole number")
        user = {"id": secret, "alterId": alter_id,
                "security": (p.get("encryption") or p.get("scy") or "auto").lower()}
        settings = {"vnext": [{"address": address, "port": port, "users": [user]}]}
        security = _security(p.get("security"), protocol, "none")
    else:
        settings = {"servers": [{"address": address, "port": port, "password": secret}]}
        security = _security(p.get("security"), protocol, "tls")     # trojan is TLS unless it says none
        if not p.get("sni") and p.get("peer"):                       # older trojan links name the SNI "peer"
            p["sni"] = p["peer"]
    stream, sni = _build_stream(protocol, network, security, p, address)
    name = _clean_name(urllib.parse.unquote(fragment))
    return _outbound(protocol, settings, stream), _meta(name, protocol, address, port, network, security, sni)


def _parse_ss(body):
    """ss:// in its three shapes: SIP002 ss://base64url(method:password)@host:port?params#name, SIP002
    with a percent-encoded ss://method:password@host:port (required for the 2022 ciphers), and the
    legacy ss://base64(method:password@host:port)#name."""
    rest, _, fragment = body.partition("#")
    rest_no_query, _, query = rest.partition("?")
    if "@" in rest_no_query:                        # SIP002 (base64 never contains "@")
        if _SPACE_RE.search(rest):
            raise ValueError("shadowsocks link: the link contains spaces or line breaks")
        userinfo, _, hostpath = rest_no_query.rpartition("@")
        hostport = hostpath.split("/", 1)[0]
        credentials = urllib.parse.unquote(userinfo)
        if ":" not in credentials:                  # base64 never contains ":" either
            credentials = _utf8(_b64decode(credentials))
            if credentials is None:
                raise ValueError("shadowsocks link: the part before '@' is neither method:password nor "
                                 "valid base64")
    else:
        decoded = _utf8(_b64decode(urllib.parse.unquote(rest_no_query)))
        if decoded is None:
            raise ValueError("shadowsocks link: the text after ss:// is not valid base64")
        credentials, at, hostport = decoded.rpartition("@")
        if not at:
            raise ValueError("shadowsocks link: the decoded text has no '@' before the server address")
    method, colon, password = credentials.partition(":")
    method = method.strip().lower()
    if not colon or not method:
        raise ValueError("shadowsocks link: the cipher method is missing (expected method:password)")
    if not password:
        raise ValueError("shadowsocks link: the password is missing")
    if method not in SS_METHODS:
        if method in SS_UNSUPPORTED_METHODS:
            raise ValueError("shadowsocks link: the cipher '%s' is not supported by xray (use one of %s)"
                             % (method, _SS_METHODS_SHOWN))
        raise ValueError("shadowsocks link: unknown cipher method (xray supports %s)" % _SS_METHODS_SHOWN)
    address, port = _host_port(hostport, "shadowsocks server")
    p = _query(query)
    plugin = p.get("plugin", "")
    if plugin:
        raise ValueError("shadowsocks link: SIP003 plugins%s are not supported by xray"
                         % _named(plugin.split(";", 1)[0].strip()))
    network = _network(p.get("type"), "shadowsocks")
    security = _security(p.get("security"), "shadowsocks", "none")
    stream, sni = _build_stream("shadowsocks", network, security, p, address)
    settings = {"servers": [{"address": address, "port": port, "method": method, "password": password}]}
    name = _clean_name(urllib.parse.unquote(fragment))
    return (_outbound("shadowsocks", settings, stream),
            _meta(name, "shadowsocks", address, port, network, security, sni))


def _network(value, protocol):
    v = (value or "").strip().lower() or "tcp"
    network = NETWORKS.get(v)
    if network is None:
        raise ValueError("%s link: unsupported transport%s (supported: %s)"
                         % (protocol, _named(v), SUPPORTED_NETWORKS))
    return network


def _security(value, protocol, default):
    v = (value.strip().lower() if isinstance(value, str) else "") or default
    if v not in SECURITIES:
        raise ValueError("%s link: unsupported security%s (supported: none, tls, reality)" % (protocol, _named(v)))
    return v


def _build_stream(protocol, network, security, p, address):
    """streamSettings from the transport and security parameters -> (streamSettings, server name).
    p holds the link's parameters with lower-cased keys (type, host, path, headertype, servicename,
    authority, mode, extra, sni, fp, alpn, allowinsecure, pbk, sid, spx)."""
    host = p.get("host") or ""
    path = p.get("path") or ""
    hosts = _split_list(host)
    stream = {"network": network}
    if network == "tcp":
        header = (p.get("headertype") or "none").strip().lower()
        if header == "http":                        # HTTP/1.1 request disguise: path and Host are lists
            request = {"path": _split_list(path) or ["/"]}
            if hosts:
                request["headers"] = {"Host": hosts}
            stream["tcpSettings"] = {"header": {"type": "http", "request": request}}
        elif header != "none":
            raise ValueError("%s link: unsupported TCP header type%s (supported: none, http)"
                             % (protocol, _named(header)))
    elif network == "ws":
        ws = {"path": path or "/"}
        if host:
            ws["headers"] = {"Host": host}
        stream["wsSettings"] = ws
    elif network == "grpc":
        authority = p.get("authority") or host
        grpc = {"serviceName": p.get("servicename") or path}
        if authority:
            grpc["authority"] = authority
        if (p.get("mode") or "").strip().lower() == "multi":
            grpc["multiMode"] = True
        stream["grpcSettings"] = grpc
        hosts = [authority] if authority else []
    elif network in ("http", "h2"):
        h2 = {"path": path or "/"}
        if hosts:
            h2["host"] = hosts
        stream["httpSettings"] = h2
    elif network == "httpupgrade":
        upgrade = {"path": path or "/"}
        if host:
            upgrade["host"] = host
        stream["httpupgradeSettings"] = upgrade
    else:                                           # xhttp / splithttp
        xhttp = {"path": path or "/"}
        if host:
            xhttp["host"] = host
        xhttp["mode"] = p.get("mode") or "auto"
        if p.get("extra"):
            extra = _json_object(p["extra"])
            if extra is None:
                raise ValueError("%s link: the xhttp 'extra' parameter is not a JSON object" % protocol)
            xhttp["extra"] = extra
        stream[network + "Settings"] = xhttp
    server_name = ""
    fingerprint = (p.get("fp") or "").lower()
    if security in ("tls", "reality"):
        server_name = p.get("sni") or (hosts[0] if hosts else "") or address
    if security == "tls":
        tls = {"serverName": server_name}
        if fingerprint:
            tls["fingerprint"] = fingerprint
        alpn = _split_list(p.get("alpn"))
        if alpn:
            tls["alpn"] = alpn
        if any(_truthy(p.get(k)) for k in ("allowinsecure", "insecure", "allow_insecure")):
            tls["allowInsecure"] = True
        stream["security"] = "tls"
        stream["tlsSettings"] = tls
    elif security == "reality":
        public_key = p.get("pbk") or p.get("publickey")
        if not public_key:
            raise ValueError("%s link: REALITY needs the server's public key (pbk), which the link does not "
                             "have" % protocol)
        reality = {"serverName": server_name, "fingerprint": fingerprint or "chrome", "publicKey": public_key}
        if p.get("sid"):
            reality["shortId"] = p["sid"]
        if p.get("spx"):
            reality["spiderX"] = p["spx"]
        stream["security"] = "reality"
        stream["realitySettings"] = reality
    return stream, server_name


def _outbound(protocol, settings, stream):
    return {"tag": PROXY_TAG, "protocol": protocol, "settings": settings, "streamSettings": stream}


def _meta(name, protocol, address, port, network, security, sni):
    return {"name": _clean_name(name), "protocol": protocol, "address": address, "port": port,
            "network": network, "security": security, "sni": sni}


# --------------------------------------------------------------------------- config edits

def _proxy_index(outbounds):
    """Index of the proxy outbound: the one tagged "proxy", else the first whose protocol carries
    traffic to a remote server (PROXY_PROTOCOLS), else None."""
    if not isinstance(outbounds, list):
        return None
    for i, ob in enumerate(outbounds):
        if isinstance(ob, dict) and ob.get("tag") == PROXY_TAG:
            return i
    for i, ob in enumerate(outbounds):
        if isinstance(ob, dict) and isinstance(ob.get("protocol"), str) \
                and ob["protocol"].strip().lower() in PROXY_PROTOCOLS:
            return i
    return None


def replace_proxy_outbound(config, outbound):
    """Put `outbound` in place of the config's proxy outbound -> (new_config, tag).

    The config is deep-copied (the argument is never modified). The outbound replaced is the one
    tagged "proxy", else the first outbound whose protocol is vmess / vless / trojan / shadowsocks /
    socks / http / wireguard. The new outbound takes that index (xray sends unrouted traffic to the
    FIRST outbound, so the position is kept) and that tag (routing rules keep pointing at it); an
    untagged one gets the tag "proxy". Without such an outbound (a freedom-only config) the new one is
    inserted at index 0 with the tag "proxy". inbounds, routing, dns, policy and every other section
    and outbound are left as they are. Raises ValueError for a config without an "outbounds" list."""
    if not isinstance(config, dict):
        raise ValueError("the xray config must be a JSON object")
    if not isinstance(config.get("outbounds"), list):
        raise ValueError('the xray config has no "outbounds" list')
    if not isinstance(outbound, dict) or not isinstance(outbound.get("protocol"), str) \
            or not outbound["protocol"].strip():
        raise ValueError("the new outbound must be an xray outbound object with a protocol")
    new_config = copy.deepcopy(config)
    new_outbound = copy.deepcopy(outbound)
    outbounds = new_config["outbounds"]
    index = _proxy_index(outbounds)
    if index is None:
        new_outbound["tag"] = PROXY_TAG
        outbounds.insert(0, new_outbound)
        return new_config, PROXY_TAG
    tag = outbounds[index].get("tag")
    if not isinstance(tag, str) or not tag.strip():
        tag = PROXY_TAG
    new_outbound["tag"] = tag
    outbounds[index] = new_outbound
    return new_config, tag


def apply_share_link(config_text, link):
    """Parse `link` and put it into the xray config -> (new_text, info), info = {"meta": the link's
    meta, "replaced_tag": the proxy outbound's tag, "proxy": the masked summary of the new outbound}.
    config_text is the config's text (or an already parsed dict). Nothing is written: check new_text
    with validate_with_xray() and write it with atomic_write_text(). Raises ValueError for a
    malformed link or config (see parse_share_link and loads_config)."""
    outbound, meta = parse_share_link(link)
    config = config_text if isinstance(config_text, dict) else loads_config(config_text)
    new_config, tag = replace_proxy_outbound(config, outbound)
    placed = dict(outbound, tag=tag)
    return dumps_config(new_config), {"meta": meta, "replaced_tag": tag, "proxy": summarize_outbound(placed)}


def _strip_json_comments(text):
    """xray reads its config with // and /* */ comments (and # lines); json does not. Comments outside
    strings are removed; line breaks are kept so a JSON error still points at the right line."""
    out = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
        elif c == "#" or (c == "/" and text.startswith("//", i)):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        elif c == "/" and text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            out.append("\n" * text.count("\n", i, end) or " ")
            i = end
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _json_load(text):
    """-> (object, None) or (None, (message, line, column)): only the safe parts of a JSON error are
    kept (a JSONDecodeError also holds the whole document)."""
    try:
        return json.loads(text), None
    except json.JSONDecodeError as e:
        return None, (e.msg, e.lineno, e.colno)
    except RecursionError:
        return None, ("nested too deeply", 1, 1)


def loads_config(text):
    """The xray config text -> dict. Accepts str or UTF-8 bytes, a leading BOM and xray's comments.
    Raises ValueError (message, line and column; never the content) for invalid JSON or a config that
    is not a JSON object."""
    if isinstance(text, (bytes, bytearray)):
        text = _utf8(text)
        if text is None:
            raise ValueError("the xray config is not UTF-8 text")
    if not isinstance(text, str):
        raise ValueError("the xray config must be text")
    text = text.lstrip("\ufeff")
    obj, error = _json_load(text)
    if error is not None and ("/" in text or "#" in text):
        obj, error = _json_load(_strip_json_comments(text))
    if error is not None:
        raise ValueError("the xray config is not valid JSON: %s (line %d, column %d)" % error)
    if not isinstance(obj, dict):
        raise ValueError("the xray config must be a JSON object")
    return obj


def dumps_config(config):
    """The config as the text written to disk: 2-space JSON, UTF-8 characters kept, final newline."""
    return json.dumps(config, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- masked views

def mask_secret(value):
    """A secret for display: its first 4 characters + "..." (one character) + its last 2 when it has at
    least MASK_PARTIAL_MIN_CHARS characters, else "****"; "" for None or ""."""
    if value is None or value == "":
        return ""
    s = value if isinstance(value, str) else str(value)
    if len(s) < MASK_PARTIAL_MIN_CHARS:
        return MASK
    return s[:4] + ELLIPSIS + s[-2:]


def _mask_value(value):
    if value is None or value == "":
        return value
    return mask_secret(value) if isinstance(value, str) else MASK


def _is_secret_key(key, value):
    if not isinstance(key, str):
        return False
    k = key.lower()
    return k in SECRET_KEYS or (k == "decryption" and not (isinstance(value, str) and value.lower() == "none"))


def _mask_in_place(node):
    if isinstance(node, dict):
        for key in list(node):
            if _is_secret_key(key, node[key]):
                node[key] = _mask_value(node[key])
            else:
                _mask_in_place(node[key])
    elif isinstance(node, list):
        for item in node:
            _mask_in_place(item)
    return node


def mask_config(config):
    """A deep copy of the config with every secret-like value masked (mask_secret): the keys id,
    password, pass (socks / http users and accounts), privateKey, psk, preSharedKey, secretKey, seed,
    mldsa65Seed, key, echServerKeys, Authorization headers and a "decryption" other than "none",
    anywhere in the tree (inbounds included). A secret that is not a string (a PEM key as a list of
    lines) becomes "****". publicKey and shortId are not secret and are kept."""
    return _mask_in_place(copy.deepcopy(config))


def _transport_view(network, stream):
    """(host, path) shown for a transport; for grpc the authority and the service name."""
    if network in ("ws", "websocket"):
        ws = _dict(stream.get("wsSettings"))
        headers = _dict(ws.get("headers"))
        return (_text(ws.get("host")) or _text(headers.get("Host")) or _text(headers.get("host")),
                _text(ws.get("path")))
    if network == "httpupgrade":
        up = _dict(stream.get("httpupgradeSettings"))
        return _text(up.get("host")), _text(up.get("path"))
    if network in ("xhttp", "splithttp"):
        xh = _dict(stream.get("xhttpSettings")) or _dict(stream.get("splithttpSettings"))
        return _text(xh.get("host")), _text(xh.get("path"))
    if network in ("http", "h2"):
        h2 = _dict(stream.get("httpSettings"))
        return ",".join(_split_list(h2.get("host"))), _text(h2.get("path"))
    if network in ("grpc", "gun"):
        grpc = _dict(stream.get("grpcSettings"))
        return _text(grpc.get("authority")), _text(grpc.get("serviceName"))
    if network in ("tcp", "raw"):
        tcp = _dict(stream.get("tcpSettings")) or _dict(stream.get("rawSettings"))
        header = _dict(tcp.get("header"))
        if _text(header.get("type")).lower() == "http":
            request = _dict(header.get("request"))
            return (",".join(_split_list(_dict(request.get("headers")).get("Host"))),
                    ",".join(_split_list(request.get("path"))))
    return "", ""


def _port_or_none(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 65535 else None
    if isinstance(value, str) and _PORT_RE.match(value.strip()):
        n = int(value.strip())
        return n if 1 <= n <= 65535 else None
    return None


def _loose_host_port(text):
    """"host:port" / "[v6]:port" without raising -> (host, port or None)."""
    t = (text or "").strip()
    if t.startswith("[") and "]" in t:
        host, _, rest = t[1:].partition("]")
        return host, _port_or_none(rest[1:]) if rest.startswith(":") else None
    host, colon, port = t.rpartition(":")
    if not colon or ":" in host:
        return t, None
    return host, _port_or_none(port)


def summarize_outbound(outbound):
    """Masked, display-safe view of an outbound: {"tag", "protocol", "address", "port", "network",
    "security", "sni", "host", "path", "flow", "id", "password", "method", "user"}. id / password are
    masked with mask_secret; host / path are the transport's Host header (grpc: authority) and path
    (grpc: service name). Never raises; unknown shapes give empty fields (port None)."""
    ob = _dict(outbound)
    protocol = _text(ob.get("protocol")).strip().lower()
    settings = _dict(ob.get("settings"))
    stream = _dict(ob.get("streamSettings"))
    out = {"tag": _text(ob.get("tag")), "protocol": protocol, "address": "", "port": None, "network": "",
           "security": "", "sni": "", "host": "", "path": "", "flow": "", "id": "", "password": "",
           "method": "", "user": ""}
    server = {}
    if protocol in ("vmess", "vless"):
        vnext = _list(settings.get("vnext"))
        server = _dict(vnext[0]) if vnext else settings           # newer xray also takes a flat form
        users = _list(server.get("users"))
        user = _dict(users[0]) if users else server
        out["id"] = mask_secret(_text(user.get("id")))
        out["flow"] = _text(user.get("flow"))
        out["method"] = _text(user.get("security" if protocol == "vmess" else "encryption"))
    elif protocol in ("trojan", "shadowsocks", "socks", "http"):
        servers = _list(settings.get("servers"))
        server = _dict(servers[0]) if servers else settings
        if protocol in ("trojan", "shadowsocks"):
            out["password"] = mask_secret(_text(server.get("password")))
            if protocol == "shadowsocks":
                out["method"] = _text(server.get("method"))
            else:
                out["flow"] = _text(server.get("flow"))
        else:
            users = _list(server.get("users"))
            user = _dict(users[0]) if users else {}
            out["user"] = _text(user.get("user"))
            out["password"] = mask_secret(_text(user.get("pass")))
    elif protocol == "wireguard":
        peers = _list(settings.get("peers"))
        host, port = _loose_host_port(_text(_dict(peers[0]).get("endpoint")) if peers else "")
        server = {"address": host, "port": port}
    out["address"] = _text(server.get("address"))
    out["port"] = _port_or_none(server.get("port"))
    if protocol in PROXY_PROTOCOLS and protocol != "wireguard":
        network = _text(stream.get("network")).strip().lower() or "tcp"
        security = _text(stream.get("security")).strip().lower() or "none"
        out["network"], out["security"] = network, security
        out["host"], out["path"] = _transport_view(network, stream)
        if security == "tls":
            out["sni"] = _text(_dict(stream.get("tlsSettings")).get("serverName"))
        elif security == "reality":
            out["sni"] = _text(_dict(stream.get("realitySettings")).get("serverName"))
    return out


def _local_only(listen):
    if not listen:
        return False                                # xray listens on 0.0.0.0 by default
    if listen.startswith("/") or listen.startswith("@") or listen.lower() == "localhost":
        return True                                 # a unix socket, or the loopback name
    try:
        return ipaddress.ip_address(listen).is_loopback
    except ValueError:
        return False


def summarize_config(config):
    """Display-safe overview: {"inbounds": [{"tag", "protocol", "listen", "port", "local_only"}],
    "outbounds": [{"tag", "protocol"}], "proxy": summarize_outbound(the proxy outbound) or None,
    "proxy_index": its index or None, "local_proxies": ["http://127.0.0.1:1081", "socks5://..."]}.
    listen "0.0.0.0" is shown when the config leaves it out (xray's default), with local_only False:
    such a proxy is reachable from other hosts. local_proxies are the URLs proxy_test() can use (an
    all-interfaces listener is reached on 127.0.0.1). Never raises."""
    cfg = _dict(config)
    inbounds, local = [], []
    for ib in _list(cfg.get("inbounds")):
        if not isinstance(ib, dict):
            continue
        protocol = _text(ib.get("protocol")).strip().lower()
        listen = _text(ib.get("listen")).strip()
        raw_port = ib.get("port")
        port = _port_or_none(raw_port)
        if port is None:
            port = raw_port if isinstance(raw_port, str) else None       # "1000-2000", "env:PORT"
        inbounds.append({"tag": _text(ib.get("tag")), "protocol": protocol, "listen": listen or "0.0.0.0",
                         "port": port, "local_only": _local_only(listen)})
        if protocol in ("http", "socks") and isinstance(port, int) and not listen.startswith(("/", "@")):
            host = "127.0.0.1" if listen in ("", "0.0.0.0", "::", "::0") else listen
            local.append("%s://%s" % ("http" if protocol == "http" else "socks5", _format_host_port(host, port)))
    outbounds = _list(cfg.get("outbounds"))
    index = _proxy_index(outbounds)
    return {"inbounds": inbounds,
            "outbounds": [{"tag": _text(o.get("tag")), "protocol": _text(o.get("protocol"))}
                          for o in outbounds if isinstance(o, dict)],
            "proxy": summarize_outbound(outbounds[index]) if index is not None else None,
            "proxy_index": index,
            "local_proxies": local}


# --------------------------------------------------------------------------- xray -test

def _collect_secrets(node, out):
    if isinstance(node, dict):
        for key, value in node.items():
            if _is_secret_key(key, value) and isinstance(value, (str, list)):
                out.update(v for v in (value if isinstance(value, list) else [value]) if isinstance(v, str))
            else:
                _collect_secrets(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_secrets(item, out)
    return out


_SECRET_PAIR_RE = re.compile(r'"(?:id|password|pass|privatekey|psk|presharedkey|secretkey|seed|mldsa65seed|'
                             r'key|echserverkeys|decryption)"\s*:\s*"((?:[^"\\\r\n]|\\.)*)"', re.I)


def _secrets_in(text):
    """The secret values of a config text: from the parsed tree, plus a regex pass that also works on
    the invalid JSON a failed validation is usually about."""
    found = set(m.group(1) for m in _SECRET_PAIR_RE.finditer(text))
    try:
        _collect_secrets(loads_config(text), found)
    except ValueError:
        pass
    return [s for s in sorted(found, key=len, reverse=True) if len(s) >= REDACT_MIN_CHARS]


def _redact(text, secrets):
    for secret in secrets:
        text = text.replace(secret, mask_secret(secret))
    return text


def _cap(text, limit):
    """At most `limit` characters: the head and the tail (xray prints the error last) around a marker."""
    if len(text) <= limit:
        return text
    marker = "\n...(output shortened)...\n"
    keep = max(0, limit - len(marker))
    head = keep // 4
    return text[:head] + marker + text[len(text) - (keep - head):]


def _decode_output(data):
    if data is None:
        return ""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
    return _ANSI_RE.sub("", text.replace("\r\n", "\n"))


def _run_xray(argv, timeout):
    env = dict(os.environ)
    for name in _XRAY_CONFIG_ENV:
        env.pop(name, None)
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        partial = _decode_output(e.output)
        return False, "%s\nxray -test did not finish within %s seconds (stopped)" % (partial.rstrip(), timeout)
    except OSError as e:
        return False, "cannot run %s: %s" % (os.path.basename(str(argv[0])), e.strerror or e)
    return proc.returncode == 0, _decode_output(proc.stdout)


def _remove_quietly(path):
    for attempt in range(5):                        # Windows: a scanner may hold the file briefly
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == 4:
                return
            time.sleep(0.05 * (attempt + 1))


def validate_with_xray(config_text, xray_argv, timeout=XRAY_TIMEOUT_S, tmp_dir=None):
    """Check a config with the xray binary -> (ok, output).

    The text (a str, UTF-8 bytes or a dict, which is dumped with dumps_config) is written to a private
    temporary file (mode 0600, in tmp_dir or the system temp directory) and checked with
    `xray_argv + ["run", "-test", "-c", path]` (xray >= 1.8); when xray answers that the command is
    unknown (an older build) it is retried with `xray_argv + ["-test", "-config", path]`. ok is exit
    status 0. output is xray's stdout and stderr, the config's secrets redacted, at most
    XRAY_OUTPUT_LIMIT characters. A binary that cannot be started or a run longer than `timeout`
    seconds gives (False, reason). The temporary file is always removed.

    xray_argv is a list (["/opt/xray/xray"]; a single path string is accepted too). The check runs as
    the calling user, who alone can read the temporary file: do not drop privileges in xray_argv."""
    if isinstance(xray_argv, str):
        xray_argv = [xray_argv]
    if not isinstance(xray_argv, (list, tuple)) or not xray_argv \
            or not all(isinstance(a, str) and a for a in xray_argv):
        raise ValueError("xray_argv must be a non-empty list of strings, like ['/opt/xray/xray']")
    if isinstance(config_text, dict):
        config_text = dumps_config(config_text)
    if isinstance(config_text, str):
        data = config_text.encode("utf-8")
    elif isinstance(config_text, (bytes, bytearray)):
        data = bytes(config_text)
    else:
        raise ValueError("config_text must be the config's text")
    secrets = _secrets_in(data.decode("utf-8", "replace"))
    fd, path = tempfile.mkstemp(prefix="xray-test-", suffix=".json", dir=tmp_dir)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(path, 0o600)                   # mkstemp already creates it 0600 on POSIX
        except OSError:
            pass
        argv = list(xray_argv)
        ok, output = _run_xray(argv + ["run", "-test", "-c", path], timeout)
        if not ok and _UNKNOWN_COMMAND_RE.search(output):
            ok, output = _run_xray(argv + ["-test", "-config", path], timeout)
            output = "(this xray does not know 'run -test'; checked with '-test -config')\n" + output
    finally:
        _remove_quietly(path)
    return ok, _cap(_redact(output.strip(), secrets), XRAY_OUTPUT_LIMIT)


# --------------------------------------------------------------------------- proxy probe

def parse_proxy_url(url):
    """"http://127.0.0.1:1081" / "socks5://[::1]:1080" -> (scheme, host, port); scheme "http" or "socks5"
    (socks5h is the same thing here: target names are always resolved by the proxy). A bare host:port
    means http. Raises ValueError for another scheme, a missing or bad port, credentials (not
    supported; never echoed), a path or a query."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("the proxy URL is empty (expected something like http://127.0.0.1:1081)")
    text = url.strip()
    m = _SCHEME_RE.match(text)
    if m:
        scheme, rest = m.group(1).lower(), text[m.end():]
    elif "://" in text:
        raise ValueError("the proxy URL has an invalid scheme (use http:// or socks5://)")
    else:
        scheme, rest = "http", text
    if scheme == "socks5h":
        scheme = "socks5"
    if scheme not in ("http", "socks5"):
        raise ValueError("unsupported proxy type '%s' (use http:// or socks5://)" % scheme)
    if rest.endswith("/"):
        rest = rest[:-1]
    if "@" in rest:
        raise ValueError("proxy URLs with a user name or password are not supported")
    if any(c in rest for c in "/?#") or _SPACE_RE.search(rest):
        raise ValueError("the proxy URL must be just scheme://host:port (no path, query or spaces)")
    host, port = _host_port(rest, "proxy")
    return scheme, host, port


class _ProbeError(Exception):
    """A proxy answer that is not a success; the message is short and ready for display."""


def _remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise socket.timeout("timed out")
    return left


def _recv_exact(sock, n, deadline):
    buf = b""
    while len(buf) < n:
        sock.settimeout(_remaining(deadline))
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise _ProbeError("the proxy closed the connection")
        buf += chunk
    return buf


def _printable(data, limit=60):
    return re.sub(r"[^\x20-\x7e]", "", data)[:limit].strip()


def _http_connect(sock, host, port, deadline):
    """HTTP CONNECT host:port; anything but a 200 answer is an error. The answer is read one byte at a
    time so no byte of the tunnel that follows is swallowed."""
    target = _format_host_port(host, port)
    request = "CONNECT %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: proxy-test\r\n\r\n" % (target, target)
    sock.settimeout(_remaining(deadline))
    sock.sendall(request.encode("ascii"))
    head = b""
    while not head.endswith(b"\r\n\r\n") and not head.endswith(b"\n\n"):
        if len(head) >= _PROXY_REPLY_MAX:
            raise _ProbeError("the proxy's answer is too long")
        sock.settimeout(_remaining(deadline))
        byte = sock.recv(1)
        if not byte:
            raise _ProbeError("the proxy closed the connection without answering" if not head else
                              "the proxy closed the connection in the middle of its answer")
        head += byte
    status = head.split(b"\n", 1)[0].rstrip(b"\r").decode("latin-1")
    m = re.match(r"^HTTP/\d(?:\.\d)?\s+(\d{3})(?:\s+(.*))?$", status)
    if not m:
        raise _ProbeError("the proxy did not answer with HTTP (is it a SOCKS proxy?)")
    if m.group(1) != "200":
        raise _ProbeError(("proxy answered %s %s" % (m.group(1), _printable(m.group(2) or ""))).strip())


def _socks5_connect(sock, host, port, deadline):
    """SOCKS5 (RFC 1928) without authentication: CONNECT to the target by name (the proxy resolves it)
    or by address."""
    sock.settimeout(_remaining(deadline))
    sock.sendall(b"\x05\x01\x00")
    version, method = _recv_exact(sock, 2, deadline)
    if version != 5:
        raise _ProbeError("the proxy did not answer as a SOCKS5 proxy (is it an HTTP proxy?)")
    if method == 0xFF:
        raise _ProbeError("the SOCKS5 proxy requires authentication")
    if method != 0:
        raise _ProbeError("the SOCKS5 proxy chose an unsupported authentication method")
    if _is_ip(host, 4):
        address = b"\x01" + ipaddress.ip_address(host).packed
    elif _is_ip(host, 6):
        address = b"\x04" + ipaddress.ip_address(host).packed
    else:
        name = host.encode("ascii")
        address = b"\x03" + struct.pack("B", len(name)) + name
    sock.settimeout(_remaining(deadline))
    sock.sendall(b"\x05\x01\x00" + address + struct.pack(">H", port))
    version, reply, _, kind = _recv_exact(sock, 4, deadline)
    if version != 5:
        raise _ProbeError("the SOCKS5 proxy sent a malformed reply")
    if reply != 0:
        raise _ProbeError(_SOCKS5_REPLIES.get(reply, "SOCKS5 error %d" % reply))
    if kind == 1:
        _recv_exact(sock, 4 + 2, deadline)
    elif kind == 4:
        _recv_exact(sock, 16 + 2, deadline)
    elif kind == 3:
        _recv_exact(sock, _recv_exact(sock, 1, deadline)[0] + 2, deadline)
    else:
        raise _ProbeError("the SOCKS5 proxy sent a malformed reply")


def _describe(error):
    """A short display text for an exception of the probe."""
    if isinstance(error, _ProbeError):
        return str(error)
    if isinstance(error, socket.timeout):
        return "timed out"
    if isinstance(error, ssl.SSLCertVerificationError):
        detail = getattr(error, "verify_message", "") or ""
        return "certificate verify failed" + (" (%s)" % detail if detail else "")
    if isinstance(error, ssl.SSLError):
        reason = getattr(error, "reason", None)
        return reason.replace("_", " ").lower() if reason else (str(error) or "TLS error")
    if isinstance(error, ConnectionRefusedError):
        return "connection refused"
    if isinstance(error, ConnectionResetError):
        return "connection reset"
    if isinstance(error, ConnectionAbortedError):
        return "connection aborted"
    if isinstance(error, socket.gaierror):
        return "cannot resolve the proxy's host name"
    if isinstance(error, OSError):
        return error.strerror or str(error) or type(error).__name__
    return "%s: %s" % (type(error).__name__, error)


def _probe(proxy, host, port, timeout, tls, label, context):
    scheme, proxy_host, proxy_port = proxy
    started = time.monotonic()
    deadline = started + timeout
    phase = "connect to proxy"
    sock = None
    error = ""
    try:
        sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
        if scheme == "http":
            phase = "HTTP CONNECT"
            _http_connect(sock, host, port, deadline)
        else:
            phase = "SOCKS5"
            _socks5_connect(sock, host, port, deadline)
        if tls:
            phase = "TLS handshake"
            if context[0] is None:
                context[0] = ssl.create_default_context()
            sock.settimeout(_remaining(deadline))
            sock = context[0].wrap_socket(sock, server_hostname=host)
    except Exception as e:                          # the probe never raises: every failure is a result
        error = ("%s: %s" % (phase, _describe(e)))[:200]
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    ms = int(round((time.monotonic() - started) * 1000))
    return {"target": label, "ok": not error, "ms": ms, "error": error}


def _targets(targets):
    if targets is None:
        return list(DEFAULT_TEST_TARGETS)
    if isinstance(targets, str):
        return [targets]
    if isinstance(targets, (tuple, list)) and len(targets) == 2 and isinstance(targets[0], str) \
            and isinstance(targets[1], int) and not isinstance(targets[1], bool):
        return [tuple(targets)]                     # one (host, port) pair instead of a list of them
    try:
        return list(targets)
    except TypeError:
        return [targets]


def _target(target):
    """-> (label, host, port, error)."""
    label = _CTRL_RE.sub("?", str(target))[:80]
    try:
        if isinstance(target, str):
            text = target.strip()
            if text.startswith("[") or text.count(":") == 1:
                host, port = _host_port(text, "target")
            else:
                host, port = _hostname(text, "target"), 443
        elif isinstance(target, (tuple, list)) and len(target) == 2 and isinstance(target[0], str):
            host, port = _hostname(target[0], "target"), _port(target[1], "target")
        else:
            return label, None, None, "invalid target (expected (host, port) or 'host:port')"
    except ValueError as e:
        return label, None, None, "invalid target: %s" % e
    return _format_host_port(host, port), host, port, ""


def proxy_test(proxy_url, targets=DEFAULT_TEST_TARGETS, timeout=PROXY_TEST_TIMEOUT_S, tls=True):
    """Probe the proxy end to end -> [{"target": "host:port", "ok": bool, "ms": int, "error": str}], one
    per target, in order.

    Through an http://host:port proxy it sends `CONNECT target:port HTTP/1.1` and requires a 200; through
    a socks5://host:port proxy it does the SOCKS5 no-auth CONNECT (the proxy resolves the name). With
    tls=True it then completes a TLS handshake with SNI = the target and certificate verification, which
    proves the whole path works (xray answers the CONNECT before it reaches the remote server). ms is
    the time the target took (connect + handshake), also on failure. `timeout` (seconds) bounds each
    target. Never raises: an invalid proxy URL or target, a refused connection, a non-200 answer, a
    SOCKS error or a TLS failure is a result with ok False and a short error such as
    "HTTP CONNECT: proxy answered 403 Forbidden" or "connect to proxy: connection refused"."""
    try:
        timeout = float(timeout)
        if not (math.isfinite(timeout) and timeout > 0):
            raise ValueError
    except (TypeError, ValueError):
        timeout = float(PROXY_TEST_TIMEOUT_S)
    proxy, proxy_error = None, ""
    try:
        proxy = parse_proxy_url(proxy_url)
    except Exception as e:
        proxy_error = "invalid proxy URL: %s" % e
    context = [None]                                # one TLS context for all targets, made when needed
    results = []
    for target in _targets(targets):
        label, host, port, target_error = _target(target)
        if proxy_error or target_error:
            results.append({"target": label, "ok": False, "ms": 0, "error": proxy_error or target_error})
        else:
            results.append(_probe(proxy, host, port, timeout, tls, label, context))
    return results


# --------------------------------------------------------------------------- atomic write

def _replace(src, dst):
    for attempt in range(5):                        # Windows: the target may be briefly locked by a scanner
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def _fsync_dir(directory):
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path, text, mode=None):
    """Replace the file at `path` with `text` (str, written as UTF-8 byte for byte, or bytes) atomically:
    a temporary file in the same directory, fsync, then os.replace, so a reader (xray at its restart)
    sees the old or the new file, never half of one. When the file exists its mode and owner are kept
    (os.chown where available; best effort when not running as root); `mode` overrides the mode; a new
    file gets `mode` or 0600 (the config holds secrets; pass 0o644 when another user must read it). A
    symlinked path is followed: the link stays and its target is replaced. On any error the temporary
    file is removed and the old file is untouched. Returns the path written."""
    if isinstance(text, str):
        data = text.encode("utf-8")
    elif isinstance(text, (bytes, bytearray)):
        data = bytes(text)
    else:
        raise TypeError("text must be str or bytes")
    target = os.path.realpath(os.fspath(path))
    directory = os.path.dirname(target)
    try:
        existing = os.stat(target)
    except FileNotFoundError:
        existing = None
    if mode is None:
        mode = stat.S_IMODE(existing.st_mode) if existing is not None else 0o600
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(target) + ".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if existing is not None and hasattr(os, "chown"):
            try:
                os.chown(tmp, existing.st_uid, existing.st_gid)
            except OSError:
                pass                                # not root: the file keeps the caller as its owner
        os.chmod(tmp, mode)                         # after chown, which may clear set-id bits
        _replace(tmp, target)
    except BaseException:
        _remove_quietly(tmp)
        raise
    _fsync_dir(directory)
    return target
