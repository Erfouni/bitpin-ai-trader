"""bitpin.vpn: share-link import, proxy outbound replacement, masked views, the xray -test wrapper, the
proxy probe and the atomic writer. Synthetic links and configs only (fake UUIDs and passwords, links
built from dicts in the tests); the only sockets are fakes the tests open on 127.0.0.1, and the "xray"
binary is a tiny Python script written to a temporary directory."""
import base64
import copy
import json
import os
import re
import shutil
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bitpin import vpn  # noqa: E402
from bitpin.vpn import (apply_share_link, atomic_write_text, dumps_config, loads_config, mask_config,  # noqa: E402
                        mask_secret, parse_proxy_url, parse_share_link, proxy_test, replace_proxy_outbound,
                        summarize_config, summarize_outbound, validate_with_xray)

UUID = "11111111-2222-3333-4444-555555555555"
UUID2 = "66666666-7777-8888-9999-aaaaaaaaaaaa"
TROJAN_PW = "Qx7Kd93LmZp20Tv5W0123"          # fake, random-looking: no word of it is a JSON key
SS_PW = "Sv8Jq41NbYc6Hr4567"
PBK = "Zm9vYmFyUHVibGljS2V5Tm90U2VjcmV0MDEyMzQ1Njc"         # a REALITY public key is not a secret
SID = "0123456789abcdef"
SECRETS = (UUID, TROJAN_PW, SS_PW)
ELL = "\u2026"


# --------------------------------------------------------------------------- link builders

def q(value):
    return urllib.parse.quote(str(value), safe="")


def b64(text, urlsafe=False, padding=True):
    raw = text.encode("utf-8") if isinstance(text, str) else text
    out = (base64.urlsafe_b64encode(raw) if urlsafe else base64.b64encode(raw)).decode("ascii")
    return out if padding else out.rstrip("=")


def url_link(scheme, userinfo, host, port, params=None, name=None):
    """scheme://userinfo@host:port?k=v&...#name with every value percent-encoded (IPv6 in brackets)."""
    authority = "[%s]:%s" % (host, port) if ":" in host else "%s:%s" % (host, port)
    link = "%s://%s@%s" % (scheme, userinfo, authority)
    if params:
        link += "?" + "&".join("%s=%s" % (k, q(v)) for k, v in params.items())
    if name is not None:
        link += "#" + q(name)
    return link


def vless_link(params=None, host="vl.example.com", port=443, uid=UUID, name="vless test"):
    return url_link("vless", q(uid), host, port, params, name)


def trojan_link(params=None, host="tr.example.com", port=443, password=TROJAN_PW, name="trojan test"):
    return url_link("trojan", q(password), host, port, params, name)


def vmess_fields(**fields):
    base = {"v": "2", "ps": "vmess test", "add": "vm.example.com", "port": "443", "id": UUID, "aid": "0",
            "scy": "auto", "net": "tcp", "type": "none", "host": "", "path": "", "tls": "", "sni": "",
            "alpn": "", "fp": ""}
    base.update(fields)
    return base


def vmess_link(fields, urlsafe=False, padding=True):
    return "vmess://" + b64(json.dumps(fields), urlsafe, padding)


def ss_link(method, password, host="ss.example.com", port=8388, name="ss test", params=None, form="sip002",
            urlsafe=True, padding=False):
    """form: "sip002" (base64 userinfo), "plain" (percent-encoded method:password) or "legacy"."""
    authority = "[%s]:%s" % (host, port) if ":" in host else "%s:%s" % (host, port)
    if form == "legacy":
        link = "ss://" + b64("%s:%s@%s" % (method, password, authority), urlsafe, padding)
    else:
        userinfo = b64(method + ":" + password, urlsafe, padding) if form == "sip002" else q(method) + ":" + q(password)
        link = "ss://%s@%s" % (userinfo, authority)
        if params:
            link += "?" + "&".join("%s=%s" % (k, q(v)) for k, v in params.items())
    return link + ("#" + q(name) if name is not None else "")


def reality_params(**extra):
    p = {"encryption": "none", "flow": "xtls-rprx-vision", "security": "reality", "sni": "www.microsoft.com",
         "fp": "firefox", "pbk": PBK, "sid": SID, "spx": "/", "type": "tcp", "headerType": "none"}
    p.update(extra)
    return p


def sample_config():
    """The shape of a typical tunnel config: HTTP inbound on 127.0.0.1:1081, a socks inbound, the proxy
    outbound first, then freedom and blackhole, routing, dns and policy sections."""
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {"tag": "http-in", "listen": "127.0.0.1", "port": 1081, "protocol": "http", "settings": {}},
            {"tag": "socks-in", "listen": "127.0.0.1", "port": 1080, "protocol": "socks",
             "settings": {"auth": "noauth", "udp": True}},
        ],
        "outbounds": [
            {"tag": "proxy", "protocol": "vmess",
             "settings": {"vnext": [{"address": "old.example.com", "port": 443,
                                     "users": [{"id": UUID2, "alterId": 0, "security": "auto"}]}]},
             "streamSettings": {"network": "ws", "security": "tls", "wsSettings": {"path": "/old"},
                                "tlsSettings": {"serverName": "old.example.com"}},
             "mux": {"enabled": False}},
            {"tag": "direct", "protocol": "freedom", "settings": {}},
            {"tag": "block", "protocol": "blackhole", "settings": {}},
        ],
        "routing": {"domainStrategy": "IPIfNonMatch",
                    "rules": [{"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"},
                              {"type": "field", "network": "tcp,udp", "outboundTag": "proxy"}]},
        "dns": {"servers": ["1.1.1.1", "8.8.8.8"]},
        "policy": {"levels": {"0": {"handshake": 4, "connIdle": 300}}},
    }


# --------------------------------------------------------------------------- share links

class LinkCase(unittest.TestCase):
    def parse(self, link):
        outbound, meta = parse_share_link(link)
        self.assertEqual(sorted(meta), ["address", "name", "network", "port", "protocol", "security", "sni"])
        self.assertEqual(outbound["tag"], "proxy")
        self.assertEqual(list(outbound), ["tag", "protocol", "settings", "streamSettings"])
        return outbound, meta

    def stream(self, link):
        return self.parse(link)[0]["streamSettings"]


class TestVlessLinks(LinkCase):
    def test_reality_vision_link_gives_the_full_outbound(self):
        name = "DE reality \U0001F1E9\U0001F1EA"
        outbound, meta = self.parse(vless_link(reality_params(), host="203.0.113.10", name=name))
        self.assertEqual(outbound, {
            "tag": "proxy", "protocol": "vless",
            "settings": {"vnext": [{"address": "203.0.113.10", "port": 443,
                                    "users": [{"id": UUID, "encryption": "none", "flow": "xtls-rprx-vision"}]}]},
            "streamSettings": {"network": "tcp", "security": "reality",
                               "realitySettings": {"serverName": "www.microsoft.com", "fingerprint": "firefox",
                                                   "publicKey": PBK, "shortId": SID, "spiderX": "/"}}})
        self.assertEqual(meta, {"name": name, "protocol": "vless",
                                "address": "203.0.113.10", "port": 443, "network": "tcp", "security": "reality",
                                "sni": "www.microsoft.com"})

    def test_reality_defaults_the_fingerprint_to_chrome_and_omits_what_is_absent(self):
        p = reality_params()
        for k in ("fp", "sid", "spx", "flow", "encryption"):
            del p[k]
        outbound, _ = self.parse(vless_link(p))
        self.assertEqual(outbound["streamSettings"]["realitySettings"],
                         {"serverName": "www.microsoft.com", "fingerprint": "chrome", "publicKey": PBK})
        self.assertEqual(outbound["settings"]["vnext"][0]["users"], [{"id": UUID, "encryption": "none"}])

    def test_ws_tls_link_with_every_tls_field(self):
        stream = self.stream(vless_link({"security": "tls", "type": "ws", "host": "cdn.example.com",
                                         "path": "/ws?ed=2048", "sni": "sni.example.com", "alpn": "h2,http/1.1",
                                         "fp": "chrome", "allowInsecure": "1"}))
        self.assertEqual(stream, {"network": "ws", "wsSettings": {"path": "/ws?ed=2048",
                                                                  "headers": {"Host": "cdn.example.com"}},
                                  "security": "tls",
                                  "tlsSettings": {"serverName": "sni.example.com", "fingerprint": "chrome",
                                                  "alpn": ["h2", "http/1.1"], "allowInsecure": True}})

    def test_tls_server_name_defaults_to_the_host_header_then_the_address(self):
        stream = self.stream(vless_link({"security": "tls", "type": "ws", "host": "cdn.example.com"}))
        self.assertEqual(stream["tlsSettings"], {"serverName": "cdn.example.com"})
        stream = self.stream(vless_link({"security": "tls", "type": "ws"}, host="edge.example.net"))
        self.assertEqual(stream["tlsSettings"], {"serverName": "edge.example.net"})
        self.assertEqual(stream["wsSettings"], {"path": "/"})
        _, meta = self.parse(vless_link({"security": "tls"}, host="198.51.100.7"))
        self.assertEqual(meta["sni"], "198.51.100.7")

    def test_allow_insecure_only_when_asked(self):
        for value, expected in (("1", True), ("true", True), ("0", None), ("false", None), ("", None)):
            with self.subTest(value=value):
                tls = self.stream(vless_link({"security": "tls", "allowInsecure": value}))["tlsSettings"]
                self.assertEqual(tls.get("allowInsecure"), expected)

    def test_every_transport(self):
        cases = [
            ({"type": "tcp"}, {"network": "tcp"}),
            ({"type": "raw"}, {"network": "tcp"}),
            ({}, {"network": "tcp"}),
            ({"type": "tcp", "headerType": "http", "host": "a.example.com,b.example.com", "path": "/p1,/p2"},
             {"network": "tcp", "tcpSettings": {"header": {"type": "http", "request": {
                 "path": ["/p1", "/p2"], "headers": {"Host": ["a.example.com", "b.example.com"]}}}}}),
            ({"type": "tcp", "headerType": "http"},
             {"network": "tcp", "tcpSettings": {"header": {"type": "http", "request": {"path": ["/"]}}}}),
            ({"type": "ws", "path": "/ws", "host": "h.example.com"},
             {"network": "ws", "wsSettings": {"path": "/ws", "headers": {"Host": "h.example.com"}}}),
            ({"type": "grpc", "serviceName": "my-svc", "mode": "multi", "authority": "auth.example.com"},
             {"network": "grpc", "grpcSettings": {"serviceName": "my-svc", "authority": "auth.example.com",
                                                  "multiMode": True}}),
            ({"type": "grpc", "serviceName": "gun-svc", "mode": "gun"},
             {"network": "grpc", "grpcSettings": {"serviceName": "gun-svc"}}),
            ({"type": "h2", "path": "/h2", "host": "a.example.com,b.example.com"},
             {"network": "h2", "httpSettings": {"path": "/h2", "host": ["a.example.com", "b.example.com"]}}),
            ({"type": "http", "path": "/h"}, {"network": "http", "httpSettings": {"path": "/h"}}),
            ({"type": "httpupgrade", "path": "/up", "host": "up.example.com"},
             {"network": "httpupgrade", "httpupgradeSettings": {"path": "/up", "host": "up.example.com"}}),
            ({"type": "xhttp", "path": "/xh", "host": "x.example.com", "mode": "packet-up"},
             {"network": "xhttp", "xhttpSettings": {"path": "/xh", "host": "x.example.com", "mode": "packet-up"}}),
            ({"type": "xhttp", "path": "/xh"}, {"network": "xhttp", "xhttpSettings": {"path": "/xh", "mode": "auto"}}),
            ({"type": "splithttp", "path": "/sp", "host": "s.example.com"},
             {"network": "splithttp", "splithttpSettings": {"path": "/sp", "host": "s.example.com", "mode": "auto"}}),
            ({"type": "xhttp", "path": "/xe", "extra": json.dumps({"xPaddingBytes": "100-1000", "noGRPCHeader": True})},
             {"network": "xhttp", "xhttpSettings": {"path": "/xe", "mode": "auto",
                                                    "extra": {"xPaddingBytes": "100-1000", "noGRPCHeader": True}}}),
        ]
        for params, expected in cases:
            with self.subTest(params=params):
                outbound, meta = self.parse(vless_link(params))
                self.assertEqual(outbound["streamSettings"], expected)
                self.assertEqual(meta["network"], expected["network"])
                self.assertEqual((meta["security"], meta["sni"]), ("none", ""))

    def test_the_tls_server_name_of_each_transport(self):
        cases = [({"type": "tcp", "headerType": "http", "host": "a.example.com,b.example.com"}, "a.example.com"),
                 ({"type": "grpc", "serviceName": "s", "authority": "auth.example.com"}, "auth.example.com"),
                 ({"type": "h2", "host": "h2.example.com"}, "h2.example.com"),
                 ({"type": "httpupgrade", "host": "up.example.com"}, "up.example.com"),
                 ({"type": "xhttp", "host": "x.example.com"}, "x.example.com"),
                 ({"type": "grpc", "serviceName": "s"}, "vl.example.com")]
        for params, sni in cases:
            with self.subTest(params=params):
                outbound, meta = self.parse(vless_link(dict(params, security="tls")))
                self.assertEqual(outbound["streamSettings"]["tlsSettings"]["serverName"], sni)
                self.assertEqual(meta["sni"], sni)

    def test_ipv6_in_brackets(self):
        outbound, meta = self.parse(vless_link({"security": "tls", "type": "ws"}, host="2001:db8::1", port=8443))
        self.assertEqual(outbound["settings"]["vnext"][0]["address"], "2001:db8::1")
        self.assertEqual((meta["address"], meta["port"]), ("2001:db8::1", 8443))
        self.assertEqual(outbound["streamSettings"]["tlsSettings"]["serverName"], "2001:db8::1")
        outbound, _ = self.parse("vless://%s@[::1]:443" % UUID)
        self.assertEqual(outbound["settings"]["vnext"][0]["address"], "::1")

    def test_url_encoded_fields_and_the_name(self):
        link = ("vless://%s@vl.example.com:2053/?type=ws&security=tls&path=%%2Fws%%2Fpath%%3Fed%%3D2048"
                "&host=cdn.example.com&sni=sni.example.com&alpn=h2%%2Chttp%%2F1.1#%%D8%%B3%%D8%%B1%%D9%%88%%D8%%B1"
                "%%20%%D8%%A2%%D9%%84%%D9%%85%%D8%%A7%%D9%%86%%20%%2B%%201" % UUID)
        outbound, meta = self.parse(link)
        self.assertEqual(outbound["streamSettings"]["wsSettings"]["path"], "/ws/path?ed=2048")
        self.assertEqual(outbound["streamSettings"]["tlsSettings"]["alpn"], ["h2", "http/1.1"])
        self.assertEqual(meta["name"], "\u0633\u0631\u0648\u0631 \u0622\u0644\u0645\u0627\u0646 + 1")
        self.assertEqual(meta["port"], 2053)

    def test_parameter_names_are_case_insensitive(self):
        link = vless_link({"TYPE": "grpc", "servicename": "svc", "MODE": "multi", "Security": "tls",
                           "allowinsecure": "true", "SNI": "s.example.com"})
        stream = self.stream(link)
        self.assertEqual(stream["grpcSettings"], {"serviceName": "svc", "multiMode": True})
        self.assertEqual(stream["tlsSettings"], {"serverName": "s.example.com", "allowInsecure": True})
        stream = self.stream(vless_link({"type": "tcp", "headertype": "HTTP"}))
        self.assertEqual(stream["tcpSettings"]["header"]["type"], "http")

    def test_encryption_and_flow(self):
        users = self.parse(vless_link({"flow": "xtls-rprx-vision"}))[0]["settings"]["vnext"][0]["users"]
        self.assertEqual(users, [{"id": UUID, "encryption": "none", "flow": "xtls-rprx-vision"}])
        users = self.parse(vless_link({"encryption": "None", "flow": "XTLS-RPRX-Vision"}))[0]["settings"]["vnext"][0][
            "users"]
        self.assertEqual(users, [{"id": UUID, "encryption": "none", "flow": "xtls-rprx-vision"}])
        stream = self.stream(vless_link(reality_params(fp="Chrome")))
        self.assertEqual(stream["realitySettings"]["fingerprint"], "chrome")
        self.assertEqual(self.stream(vless_link({"security": "tls", "fp": "FireFox"}))["tlsSettings"]["fingerprint"],
                         "firefox")
        users = self.parse(vless_link({"encryption": "mlkem768x25519plus.native.0rtt.fakeClientKey"}))[0][
            "settings"]["vnext"][0]["users"]
        self.assertEqual(users, [{"id": UUID, "encryption": "mlkem768x25519plus.native.0rtt.fakeClientKey"}])

    def test_a_pasted_link_is_tolerated(self):
        link = vless_link({"security": "tls"})
        for pasted in ("  " + link + "\n", "\ufeff\u200b" + link + "\u200b", link.replace("vless://", "VLESS://", 1),
                       link.encode("utf-8")):
            with self.subTest(pasted=pasted[:12]):
                self.assertEqual(self.parse(pasted), self.parse(link))

    def test_the_name_is_cleaned_and_bounded(self):
        _, meta = self.parse(vless_link(name="a\tb\u202ec\x00d  e"))
        self.assertEqual(meta["name"], "a b c d e")
        _, meta = self.parse(vless_link(name="x" * 500))
        self.assertEqual(meta["name"], "x" * 128)
        _, meta = self.parse(vless_link(name=None))
        self.assertEqual(meta["name"], "")
        _, meta = self.parse(vless_link(name="raw space").replace("raw%20space", "raw space"))
        self.assertEqual(meta["name"], "raw space")


class TestVmessLinks(LinkCase):
    WS_TLS = dict(net="ws", host="cdn.example.com", path="/ray", tls="tls", sni="sni.example.com", alpn="h2",
                  fp="chrome")
    EXPECTED = {"tag": "proxy", "protocol": "vmess",
                "settings": {"vnext": [{"address": "vm.example.com", "port": 443,
                                        "users": [{"id": UUID, "alterId": 0, "security": "auto"}]}]},
                "streamSettings": {"network": "ws",
                                   "wsSettings": {"path": "/ray", "headers": {"Host": "cdn.example.com"}},
                                   "security": "tls",
                                   "tlsSettings": {"serverName": "sni.example.com", "fingerprint": "chrome",
                                                   "alpn": ["h2"]}}}

    def test_standard_base64_ws_tls(self):
        outbound, meta = self.parse(vmess_link(vmess_fields(**self.WS_TLS)))
        self.assertEqual(outbound, self.EXPECTED)
        self.assertEqual(meta, {"name": "vmess test", "protocol": "vmess", "address": "vm.example.com", "port": 443,
                                "network": "ws", "security": "tls", "sni": "sni.example.com"})

    def test_base64_variants_and_numeric_fields(self):
        fields = vmess_fields(ps="?>?>?> ~~~ \u00ff\u00fe", **self.WS_TLS)
        standard = b64(json.dumps(fields))
        self.assertTrue(set("+/=") & set(standard), standard)        # the variants below really differ
        numeric = dict(fields, port=443, aid=0)
        wrapped = "\n".join(standard[i:i + 40] for i in range(0, len(standard), 40))
        for link in (vmess_link(fields), vmess_link(fields, urlsafe=True), vmess_link(fields, padding=False),
                     vmess_link(fields, urlsafe=True, padding=False), vmess_link(numeric),
                     "vmess://" + wrapped, "vmess://" + standard.replace("=", "%3D"),
                     "vmess://" + standard + "==", "vmess://" + standard + "#ignored-fragment"):
            with self.subTest(link=link[:30]):
                outbound, meta = self.parse(link)
                self.assertEqual(outbound, self.EXPECTED)
                self.assertEqual(meta["name"], "?>?>?> ~~~ \u00ff\u00fe")

    def test_tcp_with_the_http_header(self):
        stream = self.stream(vmess_link(vmess_fields(net="tcp", type="http", host="a.example.com,b.example.com",
                                                     path="/p1,/p2")))
        self.assertEqual(stream, {"network": "tcp", "tcpSettings": {"header": {"type": "http", "request": {
            "path": ["/p1", "/p2"], "headers": {"Host": ["a.example.com", "b.example.com"]}}}}})

    def test_grpc_takes_the_service_name_from_path_and_the_mode_from_type(self):
        outbound, meta = self.parse(vmess_link(vmess_fields(net="grpc", path="svc-name", type="multi",
                                                            host="auth.example.com", tls="tls")))
        self.assertEqual(outbound["streamSettings"], {
            "network": "grpc", "grpcSettings": {"serviceName": "svc-name", "authority": "auth.example.com",
                                                "multiMode": True},
            "security": "tls", "tlsSettings": {"serverName": "auth.example.com"}})
        self.assertEqual(meta["sni"], "auth.example.com")
        stream = self.stream(vmess_link(vmess_fields(net="grpc", path="svc", type="gun")))
        self.assertEqual(stream, {"network": "grpc", "grpcSettings": {"serviceName": "svc"}})

    def test_h2_httpupgrade_and_xhttp(self):
        self.assertEqual(self.stream(vmess_link(vmess_fields(net="h2", host="a.example.com", path="/h2"))),
                         {"network": "h2", "httpSettings": {"path": "/h2", "host": ["a.example.com"]}})
        self.assertEqual(self.stream(vmess_link(vmess_fields(net="httpupgrade", host="u.example.com", path="/u"))),
                         {"network": "httpupgrade", "httpupgradeSettings": {"path": "/u", "host": "u.example.com"}})
        self.assertEqual(self.stream(vmess_link(vmess_fields(net="xhttp", type="stream-one", path="/x"))),
                         {"network": "xhttp", "xhttpSettings": {"path": "/x", "mode": "stream-one"}})
        self.assertEqual(self.stream(vmess_link(vmess_fields(net="xhttp", type="none", path="/x"))),
                         {"network": "xhttp", "xhttpSettings": {"path": "/x", "mode": "auto"}})

    def test_without_tls_there_are_no_security_settings(self):
        fields = vmess_fields(net="ws", host="h.example.com", path="/p", sni="x.example.com")
        outbound, meta = self.parse(vmess_link(fields))
        self.assertEqual(outbound["streamSettings"],
                         {"network": "ws", "wsSettings": {"path": "/p", "headers": {"Host": "h.example.com"}}})
        self.assertEqual((meta["security"], meta["sni"]), ("none", ""))
        _, meta = self.parse(vmess_link(vmess_fields(tls="none")))
        self.assertEqual(meta["security"], "none")

    def test_user_fields_default_and_normalise(self):
        users = self.parse(vmess_link(vmess_fields(aid="", scy="")))[0]["settings"]["vnext"][0]["users"]
        self.assertEqual(users, [{"id": UUID, "alterId": 0, "security": "auto"}])
        users = self.parse(vmess_link(vmess_fields(aid="64", scy="AES-128-GCM")))[0]["settings"]["vnext"][0]["users"]
        self.assertEqual(users, [{"id": UUID, "alterId": 64, "security": "aes-128-gcm"}])
        fields = vmess_fields()
        del fields["aid"], fields["scy"], fields["v"], fields["ps"]
        outbound, meta = self.parse(vmess_link(fields) + "#Fallback%20name")
        self.assertEqual(outbound["settings"]["vnext"][0]["users"], [{"id": UUID, "alterId": 0, "security": "auto"}])
        self.assertEqual(meta["name"], "Fallback name")

    def test_ipv6_address_with_or_without_brackets(self):
        for add in ("2001:db8::2", "[2001:db8::2]"):
            with self.subTest(add=add):
                outbound, meta = self.parse(vmess_link(vmess_fields(add=add, tls="tls")))
                self.assertEqual(outbound["settings"]["vnext"][0]["address"], "2001:db8::2")
                self.assertEqual((meta["address"], meta["sni"]), ("2001:db8::2", "2001:db8::2"))

    def test_reality_fields_in_the_json(self):
        stream = self.stream(vmess_link(vmess_fields(tls="reality", sni="www.example.com", pbk=PBK, sid=SID)))
        self.assertEqual(stream["realitySettings"], {"serverName": "www.example.com", "fingerprint": "chrome",
                                                     "publicKey": PBK, "shortId": SID})

    def test_the_url_form(self):
        link = url_link("vmess", UUID, "vm.example.com", 443, {"encryption": "aes-128-gcm", "security": "tls",
                                                                "type": "ws", "path": "/v", "host": "h.example.com"},
                        "vmess url")
        outbound, meta = self.parse(link)
        self.assertEqual(outbound["settings"], {"vnext": [{"address": "vm.example.com", "port": 443, "users": [
            {"id": UUID, "alterId": 0, "security": "aes-128-gcm"}]}]})
        self.assertEqual(outbound["streamSettings"]["wsSettings"], {"path": "/v", "headers": {"Host": "h.example.com"}})
        self.assertEqual((meta["name"], meta["sni"]), ("vmess url", "h.example.com"))


class TestTrojanLinks(LinkCase):
    def test_trojan_is_tls_by_default(self):
        outbound, meta = self.parse(trojan_link({"sni": "sni.example.com"}))
        self.assertEqual(outbound, {"tag": "proxy", "protocol": "trojan",
                                    "settings": {"servers": [{"address": "tr.example.com", "port": 443,
                                                              "password": TROJAN_PW}]},
                                    "streamSettings": {"network": "tcp", "security": "tls",
                                                       "tlsSettings": {"serverName": "sni.example.com"}}})
        self.assertEqual(meta, {"name": "trojan test", "protocol": "trojan", "address": "tr.example.com", "port": 443,
                                "network": "tcp", "security": "tls", "sni": "sni.example.com"})
        self.assertEqual(self.stream(trojan_link())["tlsSettings"], {"serverName": "tr.example.com"})
        self.assertEqual(self.stream(trojan_link({"security": ""}))["security"], "tls")

    def test_security_none_and_reality(self):
        self.assertEqual(self.stream(trojan_link({"security": "none"})), {"network": "tcp"})
        stream = self.stream(trojan_link({"security": "reality", "type": "grpc", "serviceName": "g",
                                          "sni": "r.example.com", "pbk": PBK, "sid": SID}))
        self.assertEqual(stream, {"network": "grpc", "grpcSettings": {"serviceName": "g"}, "security": "reality",
                                  "realitySettings": {"serverName": "r.example.com", "fingerprint": "chrome",
                                                      "publicKey": PBK, "shortId": SID}})

    def test_ws_tls_takes_the_host_header_as_server_name(self):
        stream = self.stream(trojan_link({"type": "ws", "host": "cdn.example.com", "path": "/tr", "fp": "safari"}))
        self.assertEqual(stream, {"network": "ws",
                                  "wsSettings": {"path": "/tr", "headers": {"Host": "cdn.example.com"}},
                                  "security": "tls",
                                  "tlsSettings": {"serverName": "cdn.example.com", "fingerprint": "safari"}})

    def test_passwords_with_special_characters(self):
        for password in ("p@ss:w/rd#?%&=+ word", "\u0631\u0645\u0632-\u0639\u0628\u0648\u0631-1234567890",
                         "x" * 64, TROJAN_PW + "@@"):
            with self.subTest(password=password):
                outbound, _ = self.parse(trojan_link(password=password))
                self.assertEqual(outbound["settings"]["servers"][0]["password"], password)
        # a raw "@" or "/" some generators leave unencoded: the LAST "@" ends the password
        outbound, meta = self.parse("trojan://ab/c@d@tr.example.com:443?security=tls#raw")
        self.assertEqual(outbound["settings"]["servers"][0]["password"], "ab/c@d")
        self.assertEqual((meta["address"], meta["port"]), ("tr.example.com", 443))

    def test_the_older_peer_parameter_is_the_sni(self):
        self.assertEqual(self.stream(trojan_link({"peer": "peer.example.com"}))["tlsSettings"]["serverName"],
                         "peer.example.com")
        self.assertEqual(self.stream(trojan_link({"peer": "peer.example.com", "sni": "sni.example.com"}))[
            "tlsSettings"]["serverName"], "sni.example.com")

    def test_ipv6(self):
        outbound, meta = self.parse(trojan_link(host="2001:db8::3", port=8443))
        self.assertEqual(outbound["settings"]["servers"][0]["address"], "2001:db8::3")
        self.assertEqual((meta["port"], meta["sni"]), (8443, "2001:db8::3"))


class TestShadowsocksLinks(LinkCase):
    EXPECTED = {"tag": "proxy", "protocol": "shadowsocks",
                "settings": {"servers": [{"address": "ss.example.com", "port": 8388, "method": "aes-256-gcm",
                                          "password": SS_PW}]},
                "streamSettings": {"network": "tcp"}}

    def test_sip002_base64_userinfo(self):
        for urlsafe, padding in ((True, False), (True, True), (False, True), (False, False)):
            with self.subTest(urlsafe=urlsafe, padding=padding):
                outbound, meta = self.parse(ss_link("aes-256-gcm", SS_PW, urlsafe=urlsafe, padding=padding))
                self.assertEqual(outbound, self.EXPECTED)
                self.assertEqual(meta, {"name": "ss test", "protocol": "shadowsocks", "address": "ss.example.com",
                                        "port": 8388, "network": "tcp", "security": "none", "sni": ""})
        link = ss_link("aes-256-gcm", SS_PW, padding=True)
        self.assertEqual(self.parse(link.replace("=", "%3D"))[0], self.EXPECTED)
        self.assertEqual(self.parse(link.replace("@ss.example.com:8388", "@ss.example.com:8388/"))[0], self.EXPECTED)

    def test_sip002_percent_encoded_method_and_password(self):
        password = "c2VydmVyLWtleS0xMjM0NQ+/==:dXNlci1rZXktNjc4OTA="     # 2022 ciphers: server key:user key
        outbound, _ = self.parse(ss_link("2022-blake3-aes-128-gcm", password, form="plain"))
        self.assertEqual(outbound["settings"]["servers"][0]["method"], "2022-blake3-aes-128-gcm")
        self.assertEqual(outbound["settings"]["servers"][0]["password"], password)
        outbound, _ = self.parse("ss://chacha20-ietf-poly1305:%s@ss.example.com:8388" % SS_PW)
        self.assertEqual(outbound["settings"]["servers"][0]["password"], SS_PW)

    def test_the_legacy_form(self):
        outbound, meta = self.parse(ss_link("chacha20-ietf-poly1305", "pass@word:x", form="legacy", name="legacy"))
        self.assertEqual(outbound["settings"], {"servers": [{"address": "ss.example.com", "port": 8388,
                                                             "method": "chacha20-ietf-poly1305",
                                                             "password": "pass@word:x"}]})
        self.assertEqual(meta["name"], "legacy")
        outbound, meta = self.parse(ss_link("aes-128-gcm", SS_PW, host="2001:db8::5", form="legacy", padding=True))
        self.assertEqual((meta["address"], meta["port"]), ("2001:db8::5", 8388))
        self.assertEqual(outbound["settings"]["servers"][0]["password"], SS_PW)

    def test_the_method_is_case_insensitive_and_ipv6_works(self):
        outbound, meta = self.parse(ss_link("AES-128-GCM", SS_PW, host="2001:db8::4"))
        self.assertEqual(outbound["settings"]["servers"][0], {"address": "2001:db8::4", "port": 8388,
                                                              "method": "aes-128-gcm", "password": SS_PW})

    def test_a_transport_parameter_is_honoured(self):
        stream = self.stream(ss_link("aes-128-gcm", SS_PW, params={"type": "ws", "path": "/ss", "host": "h.example.com",
                                                                    "security": "tls"}))
        self.assertEqual(stream, {"network": "ws", "wsSettings": {"path": "/ss", "headers": {"Host": "h.example.com"}},
                                  "security": "tls", "tlsSettings": {"serverName": "h.example.com"}})
        self.assertEqual(self.stream(ss_link("aes-128-gcm", SS_PW, params={"plugin": ""})), {"network": "tcp"})


class TestMalformedLinks(unittest.TestCase):
    """Every rejection is a ValueError with an English message that never contains a secret or a piece
    of the link, and carries no chained exception (a decoded vmess JSON would hold the user id)."""

    def reject(self, link, *expected):
        with self.assertRaises(ValueError) as cm:
            parse_share_link(link)
        e = cm.exception
        message = str(e)
        dump = "".join(traceback.format_exception(type(e), e, e.__traceback__)) + repr(e.args)
        for secret in SECRETS + (q(TROJAN_PW), b64(UUID)[:20]):
            self.assertNotIn(secret, dump)
        if isinstance(link, str) and "://" in link:
            body = link.split("://", 1)[1]
            for i in range(0, max(0, len(body) - 15)):
                self.assertNotIn(body[i:i + 16], message)
        self.assertIsNone(e.__cause__)
        self.assertIsNone(e.__context__)
        self.assertTrue(message[:1].isalpha(), message)
        for part in expected:
            self.assertIn(part, message)
        return message

    def test_bad_vmess_payloads(self):
        self.reject("vmess://!!!not*base64!!!", "vmess link", "not valid base64")
        self.reject("vmess://" + "A", "not valid base64")
        self.reject("vmess://" + b64("not json " + UUID), "does not decode to a JSON object")
        self.reject("vmess://" + b64(json.dumps([UUID])), "does not decode to a JSON object")
        self.reject("vmess://" + b64(b"\xff\xfe" + UUID.encode()), "does not decode to a JSON object")
        self.reject("vmess://" + b64("[" * 5000 + "]" * 5000), "does not decode to a JSON object")
        self.reject(vmess_link(vmess_fields(id="")), "user id (id) is missing")
        self.reject(vmess_link(vmess_fields(port="")), "vmess server port is missing")
        self.reject(vmess_link(vmess_fields(port="99999")), "between 1 and 65535")
        self.reject(vmess_link(vmess_fields(port=True)), "between 1 and 65535")
        self.reject(vmess_link(vmess_fields(add="")), "vmess server address is missing")
        self.reject(vmess_link(vmess_fields(add="bad host!")), "not a valid host name")
        self.reject(vmess_link(vmess_fields(aid="x")), "alterId (aid)")
        self.reject(vmess_link(vmess_fields(net="kcp")), "unsupported transport 'kcp'", "supported: tcp, ws")
        self.reject(vmess_link(vmess_fields(tls="xtls")), "unsupported security 'xtls'")

    def test_missing_or_bad_ports_and_addresses(self):
        u = UUID
        self.reject("vless://%s@vl.example.com?type=ws" % u, "vless server port is missing")
        self.reject("vless://%s@vl.example.com:" % u, "vless server port is missing")
        self.reject("vless://%s@vl.example.com:0" % u, "between 1 and 65535")
        self.reject("vless://%s@vl.example.com:70000" % u, "between 1 and 65535")
        self.reject("vless://%s@vl.example.com:44x3" % u, "between 1 and 65535")
        self.reject("vless://%s@vl.example.com:\u06f4\u06f4\u06f3" % u, "between 1 and 65535")   # Persian digits
        self.reject("vless://%s@:443" % u, "address is missing")
        self.reject("vless://%s@2001:db8::1:443" % u, "in brackets")
        self.reject("vless://%s@[2001:db8::1:443" % u, "unclosed '['")
        self.reject("vless://%s@[zz::1]:443" % u, "not an IPv6 address")
        self.reject("vless://%s@[2001:db8::1]" % u, "port is missing")
        self.reject("vless://%s@[2001:db8::1]x:443" % u, "not ':port'")
        self.reject("vless://%s@exa$mple.com:443" % u, "not a valid host name")
        self.reject("vless://%s@999.1.1.1:443" % u, "not a valid host name or IP address")
        self.reject("trojan://%s@tr.example.com:65536" % q(TROJAN_PW), "trojan server port")
        self.reject(ss_link("aes-256-gcm", SS_PW, port=0), "shadowsocks server port")

    def test_missing_credentials(self):
        self.reject("vless://vl.example.com:443?type=ws", "vless link", "user id before '@' is missing")
        self.reject("vless://@vl.example.com:443", "user id before '@' is missing")
        self.reject("vless://%20@vl.example.com:443", "empty or has control characters")
        self.reject("trojan://@tr.example.com:443", "trojan link", "password before '@' is missing")
        self.reject("trojan://%s%%0A@tr.example.com:443" % q(TROJAN_PW), "control characters")

    def test_unsupported_transport_security_and_options(self):
        for net in ("kcp", "mkcp", "quic", "domainsocket"):
            self.reject(vless_link({"type": net}), "unsupported transport '%s'" % net)
        message = self.reject(vless_link({"type": UUID}), "unsupported transport (supported")   # not echoed
        self.assertNotIn("1111", message)
        for value in (UUID.replace("-", ""), "12345678", "a" * 17, "0123456789abcdef0123456789abcdef",
                      "ws.example.com"):
            message = self.reject(vless_link({"type": value}), "unsupported transport (supported")
            self.assertNotIn(value, message)
        self.reject(vless_link({"type": "Grpc-Multi"}), "unsupported transport 'grpc-multi'")
        self.reject(vless_link({"type": "a b\nc"}), "unsupported transport (supported")
        self.reject(vless_link({"security": "xtls"}), "unsupported security 'xtls'")
        self.reject(trojan_link({"security": "ssl3"}), "trojan link", "unsupported security 'ssl3'")
        self.reject(vless_link({"security": "reality", "sni": "r.example.com"}), "public key (pbk)")
        self.reject(vless_link({"type": "tcp", "headerType": "srtp"}), "unsupported TCP header type 'srtp'")
        self.reject(vless_link({"type": "xhttp", "extra": "{not json"}), "'extra' parameter is not a JSON object")
        self.reject(vless_link({"type": "xhttp", "extra": "[1, 2]"}), "'extra' parameter is not a JSON object")

    def test_spaces_inside_a_link(self):
        self.reject("vless://%s@vl.example.com:443?type=ws&path=/a b" % UUID, "spaces or line breaks")
        self.reject("ss://%s@ss.example.com:8388?type=tcp&x=a b" % b64("aes-256-gcm:" + SS_PW), "spaces or line breaks")

    def test_bad_shadowsocks_links(self):
        self.reject("ss://!!!@ss.example.com:8388", "neither method:password nor valid base64")
        self.reject("ss://%s@ss.example.com:8388" % b64("aes-256-gcm" + SS_PW), "cipher method is missing")
        self.reject("ss://%%%%%%", "not valid base64")
        self.reject("ss://" + b64("aes-256-gcm:" + SS_PW), "no '@' before the server address")
        self.reject("ss://:%s@ss.example.com:8388" % q(SS_PW), "cipher method is missing")
        self.reject("ss://" + b64("aes-256-gcm:@ss.example.com:8388"), "password is missing")
        self.reject(ss_link("aes-256-cfb", SS_PW), "cipher 'aes-256-cfb' is not supported by xray", "aes-128-gcm")
        self.reject(ss_link("rc4-md5", SS_PW, form="legacy"), "cipher 'rc4-md5' is not supported")
        message = self.reject(ss_link("mystery-cipher", SS_PW), "unknown cipher method")
        self.assertNotIn("mystery", message)
        self.reject(ss_link("aes-128-gcm", SS_PW, params={"plugin": "obfs-local;obfs=http;obfs-host=www.bing.com"}),
                    "SIP003 plugins 'obfs-local' are not supported")
        self.reject(ss_link("aes-128-gcm", SS_PW, params={"type": "kcp"}), "shadowsocks link", "'kcp'")

    def test_not_a_supported_link_at_all(self):
        self.reject("hysteria2://%s@h.example.com:443" % UUID, "unsupported share link type 'hysteria2'")
        self.reject("https://example.com/sub/%s" % UUID, "unsupported share link type 'https'")
        self.reject("SOCKS://user:%s@s.example.com:1080" % q(TROJAN_PW), "unsupported share link type 'socks'")
        self.reject(UUID, "not a share link")
        self.reject("just some text", "not a share link")
        self.reject(b64(json.dumps(vmess_fields())), "not a share link")
        for empty in ("", "   \n\t", "\ufeff\u200b"):
            self.reject(empty, "the share link is empty")
        self.reject(None, "must be text")
        self.reject(12345, "must be text")
        self.reject(b"\xff\xfe" + UUID.encode(), "not UTF-8")
        self.reject(vless_link() + "\n" + trojan_link(), "holds 2 share links")
        self.reject("vless://%s@vl.example.com:443?path=" % UUID + "a" * 20000, "too long")


# --------------------------------------------------------------------------- config edits

class TestReplaceProxyOutbound(unittest.TestCase):
    def setUp(self):
        self.new, _ = parse_share_link(vless_link(reality_params(), host="203.0.113.10"))

    def test_the_proxy_tagged_outbound_is_replaced_in_place(self):
        config = sample_config()
        before = json.dumps(config, sort_keys=True)
        new_config, tag = replace_proxy_outbound(config, self.new)
        self.assertEqual(tag, "proxy")
        self.assertEqual(json.dumps(config, sort_keys=True), before)             # the argument is untouched
        self.assertEqual(new_config["outbounds"][0], dict(self.new, tag="proxy"))
        self.assertEqual(new_config["outbounds"][1:], config["outbounds"][1:])
        for section in ("log", "inbounds", "routing", "dns", "policy"):
            self.assertEqual(new_config[section], config[section], section)
        self.assertEqual(list(new_config), list(config))
        self.assertNotIn("mux", new_config["outbounds"][0])                     # nothing of the old outbound

    def test_the_position_is_kept_when_the_proxy_is_not_first(self):
        config = sample_config()
        proxy = config["outbounds"].pop(0)
        config["outbounds"].insert(1, proxy)                                   # direct, proxy, block
        new_config, tag = replace_proxy_outbound(config, self.new)
        self.assertEqual([o["tag"] for o in new_config["outbounds"]], ["direct", "proxy", "block"])
        self.assertEqual(new_config["outbounds"][1]["protocol"], "vless")

    def test_the_proxy_tag_wins_over_an_earlier_proxy_protocol(self):
        config = sample_config()
        config["outbounds"].insert(0, {"tag": "backup", "protocol": "trojan", "settings": {"servers": []}})
        new_config, tag = replace_proxy_outbound(config, self.new)
        self.assertEqual(tag, "proxy")
        self.assertEqual([o["protocol"] for o in new_config["outbounds"]], ["trojan", "vless", "freedom", "blackhole"])

    def test_an_untagged_first_outbound_is_replaced_and_tagged_proxy(self):
        config = {"outbounds": [{"protocol": "vless", "settings": {"vnext": []}},
                                {"tag": "direct", "protocol": "freedom"}]}
        new_config, tag = replace_proxy_outbound(config, self.new)
        self.assertEqual(tag, "proxy")
        self.assertEqual(new_config["outbounds"][0]["tag"], "proxy")
        self.assertEqual(new_config["outbounds"][0]["protocol"], "vless")
        self.assertEqual(new_config["outbounds"][1], {"tag": "direct", "protocol": "freedom"})
        self.assertEqual(len(new_config["outbounds"]), 2)

    def test_the_first_proxy_protocol_outbound_keeps_its_own_tag(self):
        config = {"outbounds": [{"tag": "direct", "protocol": "freedom"},
                                {"tag": "vpn-out", "protocol": "Shadowsocks", "settings": {}},
                                {"tag": "second", "protocol": "vmess"}],
                  "routing": {"rules": [{"type": "field", "port": "0-65535", "outboundTag": "vpn-out"}]}}
        new_config, tag = replace_proxy_outbound(config, self.new)
        self.assertEqual(tag, "vpn-out")
        self.assertEqual([o["tag"] for o in new_config["outbounds"]], ["direct", "vpn-out", "second"])
        self.assertEqual(new_config["outbounds"][1]["protocol"], "vless")
        self.assertEqual(new_config["routing"], config["routing"])
        for protocol in ("socks", "http", "wireguard", "trojan"):
            with self.subTest(protocol=protocol):
                cfg = {"outbounds": [{"tag": "direct", "protocol": "freedom"}, {"tag": "up", "protocol": protocol}]}
                self.assertEqual(replace_proxy_outbound(cfg, self.new)[1], "up")

    def test_a_freedom_only_config_gets_the_proxy_first(self):
        config = {"inbounds": [{"port": 1081, "protocol": "http"}],
                  "outbounds": [{"tag": "direct", "protocol": "freedom"}, {"tag": "dns-out", "protocol": "dns"},
                                {"tag": "block", "protocol": "blackhole"}, "not an object"]}
        new_config, tag = replace_proxy_outbound(config, self.new)
        self.assertEqual(tag, "proxy")
        self.assertEqual([o["tag"] if isinstance(o, dict) else o for o in new_config["outbounds"]],
                         ["proxy", "direct", "dns-out", "block", "not an object"])
        new_config, tag = replace_proxy_outbound({"outbounds": []}, self.new)
        self.assertEqual((new_config["outbounds"], tag), ([dict(self.new, tag="proxy")], "proxy"))

    def test_a_config_without_an_outbounds_list_is_refused(self):
        for config in ({}, {"inbounds": []}, {"outbounds": {"tag": "proxy"}}, {"outbounds": None}, [], "{}", None):
            with self.subTest(config=config):
                with self.assertRaises(ValueError) as cm:
                    replace_proxy_outbound(config, self.new)
                self.assertIn("outbounds" if isinstance(config, dict) else "JSON object", str(cm.exception))
        for outbound in (None, {}, {"protocol": ""}, {"protocol": 5}, "vless"):
            with self.assertRaises(ValueError):
                replace_proxy_outbound(sample_config(), outbound)

    def test_the_result_shares_nothing_with_the_arguments(self):
        config = sample_config()
        new_config, _ = replace_proxy_outbound(config, self.new)
        new_config["outbounds"][0]["settings"]["vnext"][0]["port"] = 1
        new_config["outbounds"][1]["settings"]["x"] = 1
        new_config["routing"]["rules"].append({})
        self.assertEqual(self.new["settings"]["vnext"][0]["port"], 443)
        self.assertEqual(config, sample_config())
        self.assertEqual(self.new["tag"], "proxy")

    def test_apply_share_link_edits_the_config_text(self):
        text = "// a tunnel\n" + dumps_config(sample_config())
        new_text, info = apply_share_link(text, vless_link(reality_params(), host="203.0.113.10", name="new server"))
        new_config = loads_config(new_text)
        self.assertTrue(new_text.endswith("}\n"))
        self.assertEqual(new_config["outbounds"][0], dict(self.new, tag="proxy"))
        self.assertEqual(new_config["routing"], sample_config()["routing"])
        self.assertEqual(info["replaced_tag"], "proxy")
        self.assertEqual(info["meta"]["name"], "new server")
        self.assertEqual((info["proxy"]["protocol"], info["proxy"]["id"]), ("vless", "1111" + ELL + "55"))
        self.assertNotIn(UUID, json.dumps(info))
        with self.assertRaises(ValueError):
            apply_share_link(text, "vless://broken")
        with self.assertRaises(ValueError):
            apply_share_link("{not json", vless_link())
        new_text, info = apply_share_link(sample_config(), trojan_link())
        self.assertEqual(loads_config(new_text)["outbounds"][0]["protocol"], "trojan")


class TestConfigText(unittest.TestCase):
    def test_comments_bom_and_bytes(self):
        text = ("\ufeff// xray config\n{\n  # a hash comment\n  \"log\": {\"loglevel\": \"warning\"}, /* a block\n"
                "  comment */\n  \"outbounds\": [{\"protocol\": \"freedom\", \"settings\": {\"note\": "
                "\"http://x/*y*/ #z //w \\\" q\"}}]\n}\n")
        config = loads_config(text)
        self.assertEqual(config["outbounds"][0]["settings"]["note"], "http://x/*y*/ #z //w \" q")
        self.assertEqual(config["log"], {"loglevel": "warning"})
        self.assertEqual(loads_config(text.encode("utf-8")), config)
        self.assertEqual(loads_config('{"a": "b"}'), {"a": "b"})

    def test_errors_name_the_place_never_the_content(self):
        text = '{\n  "outbounds": [{"settings": {"password": "%s"}}]\n  "x": 1\n}' % TROJAN_PW
        with self.assertRaises(ValueError) as cm:
            loads_config(text)
        self.assertIn("line 3", str(cm.exception))
        self.assertNotIn(TROJAN_PW, str(cm.exception))
        self.assertIsNone(cm.exception.__context__)
        text = '{\n  // comment\n  "a": 1,\n  /* two\n lines */ "b": \n}'
        with self.assertRaises(ValueError) as cm:
            loads_config(text)
        self.assertIn("line 6", str(cm.exception))
        for bad, part in (("[1, 2]", "JSON object"), ('"text"', "JSON object"), (b"\xff{}", "UTF-8"),
                          (None, "must be text"), ("", "not valid JSON"), ("[" * 100000, "not valid JSON")):
            with self.subTest(bad=repr(bad)[:20]):
                with self.assertRaises(ValueError) as cm:
                    loads_config(bad)
                self.assertIn(part, str(cm.exception))

    def test_dumps_config_keeps_unicode_and_ends_with_a_newline(self):
        text = dumps_config({"remarks": "\u0633\u0631\u0648\u0631", "n": [1, 2]})
        self.assertEqual(text, '{\n  "remarks": "\u0633\u0631\u0648\u0631",\n  "n": [\n    1,\n    2\n  ]\n}\n')
        self.assertEqual(loads_config(text), {"remarks": "\u0633\u0631\u0648\u0631", "n": [1, 2]})


# --------------------------------------------------------------------------- masked views

def secret_config():
    """Every kind of secret an xray config can hold, inbounds included."""
    return {
        "inbounds": [
            {"tag": "http-in", "listen": "127.0.0.1", "port": 1081, "protocol": "http",
             "settings": {"accounts": [{"user": "owner", "pass": "Hp3Rk8Wm2Xq5Nz7Y"}]}},
            {"tag": "vless-in", "port": 8443, "protocol": "vless",
             "settings": {"clients": [{"id": UUID2, "flow": "xtls-rprx-vision"}], "decryption": "none"},
             "streamSettings": {"network": "tcp", "security": "reality", "realitySettings": {
                 "dest": "www.example.com:443", "serverNames": ["www.example.com"],
                 "privateKey": "Pk9Lm2Qw8Er4Ty6UiXYZ", "shortIds": ["", SID],
                 "mldsa65Seed": "Md3Sd8Qe2Rt6Yu1Io4"}}},
            {"tag": "trojan-in", "port": 8444, "protocol": "trojan",
             "settings": {"clients": [{"password": "Ti4Bn7Vc1Xz3Mq8Lp"}]},
             "streamSettings": {"security": "tls", "tlsSettings": {"certificates": [{
                 "certificate": ["-----BEGIN CERTIFICATE-----", "MIIBnotasecretcert"],
                 "key": ["-----BEGIN PRIVATE KEY-----", "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC",
                         "-----END PRIVATE KEY-----"]}]}}},
            {"tag": "vless-enc-in", "port": 8445, "protocol": "vless",
             "settings": {"clients": [], "decryption": "mlkem768x25519plus.native.600s.Dc5Rv9Tb2Nm7Kx4Wq"}},
        ],
        "outbounds": [
            parse_share_link(vless_link(reality_params(), host="203.0.113.10"))[0],
            dict(parse_share_link(trojan_link({"type": "ws", "host": "cdn.example.com", "path": "/tr"}))[0], tag="t"),
            dict(parse_share_link(ss_link("aes-256-gcm", SS_PW))[0], tag="s"),
            {"tag": "socks-up", "protocol": "socks", "settings": {"servers": [{
                "address": "10.0.0.2", "port": 1080, "users": [{"user": "sockuser", "pass": "Sk5Pw9Lx3Mn7Qb2R777"}]}]}},
            {"tag": "http-up", "protocol": "http", "settings": {"servers": [{
                "address": "10.0.0.3", "port": 3128, "users": [{"user": "httpuser", "pass": "Hu6Yt2Re8Wq4Za1X888"}]}]}},
            {"tag": "wg", "protocol": "wireguard", "settings": {
                "secretKey": "Wg3Sk8Nm1Qa6Zx9Cv4Bn2Mk7Lp5Oi0UyGH", "address": ["10.8.0.2/32"],
                "peers": [{"publicKey": "wgPeerPublicKeyIsNotSecret", "preSharedKey": "Ps4Kf7Jd2Hs9Ga1Lq6OP",
                           "endpoint": "[2001:db8::9]:51820"}]}},
            {"tag": "kcp", "protocol": "vmess", "settings": {"vnext": []},
             "streamSettings": {"network": "kcp", "kcpSettings": {"seed": "Kc8Sd3Fg6Hj1Kl4ZxRS"}}},
            {"tag": "wsauth", "protocol": "vless", "settings": {"vnext": []},
             "streamSettings": {"network": "ws", "wsSettings": {"headers": {
                 "Authorization": "Basic QXV0aDpUb2tlbjEyMwYZ"}}}},
            {"tag": "direct", "protocol": "freedom"},
        ],
    }


CONFIG_SECRETS = (UUID, UUID2, TROJAN_PW, SS_PW, "Hp3Rk8Wm2Xq5Nz7Y", "Pk9Lm2Qw8Er4Ty6UiXYZ",
                  "Ti4Bn7Vc1Xz3Mq8Lp", "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC", "Sk5Pw9Lx3Mn7Qb2R777",
                  "Hu6Yt2Re8Wq4Za1X888", "Wg3Sk8Nm1Qa6Zx9Cv4Bn2Mk7Lp5Oi0UyGH", "Ps4Kf7Jd2Hs9Ga1Lq6OP",
                  "Kc8Sd3Fg6Hj1Kl4ZxRS", "Basic QXV0aDpUb2tlbjEyMwYZ", "Md3Sd8Qe2Rt6Yu1Io4",
                  "Dc5Rv9Tb2Nm7Kx4Wq")


def paths(node, prefix=""):
    """Every key path of a JSON tree (the structure mask_config must keep)."""
    out = set()
    if isinstance(node, dict):
        for k, v in node.items():
            out.add(prefix + "/" + k)
            out |= paths(v, prefix + "/" + k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out |= paths(v, "%s[%d]" % (prefix, i))
    return out


class TestMasking(unittest.TestCase):
    def assertNoSecret(self, text, secrets=CONFIG_SECRETS):
        for secret in secrets:
            self.assertNotIn(secret, text)
            for i in range(len(secret) - 6):                # at most 6 characters of a secret are shown
                self.assertNotIn(secret[i:i + 7], text, secret)

    def test_mask_secret(self):
        self.assertEqual(mask_secret(UUID), "1111" + ELL + "55")
        self.assertEqual(mask_secret(TROJAN_PW), "Qx7K" + ELL + "23")
        self.assertEqual(mask_secret("x" * 16), "xxxx" + ELL + "xx")
        for short in ("a", "short-pw", "x" * 15, 12345):
            self.assertEqual(mask_secret(short), "****")
        self.assertEqual(mask_secret(""), "")
        self.assertEqual(mask_secret(None), "")

    def test_mask_config_masks_every_secret_and_nothing_else(self):
        config = secret_config()
        original = copy.deepcopy(config)
        masked = mask_config(config)
        self.assertEqual(config, original)                                     # a deep copy
        dump = json.dumps(masked, ensure_ascii=False)
        self.assertNoSecret(dump)
        self.assertNoSecret(json.dumps(masked))
        self.assertEqual(paths(masked), paths(config))                         # same structure
        out = masked["outbounds"]
        self.assertEqual(out[0]["settings"]["vnext"][0]["users"][0]["id"], "1111" + ELL + "55")
        self.assertEqual(out[0]["streamSettings"]["realitySettings"]["publicKey"], PBK)       # not secret
        self.assertEqual(out[0]["streamSettings"]["realitySettings"]["shortId"], SID)
        self.assertEqual(out[1]["settings"]["servers"][0]["password"], "Qx7K" + ELL + "23")
        self.assertEqual(out[3]["settings"]["servers"][0]["users"][0],
                         {"user": "sockuser", "pass": "Sk5P" + ELL + "77"})
        self.assertEqual(out[5]["settings"]["peers"][0]["publicKey"], "wgPeerPublicKeyIsNotSecret")
        self.assertEqual(out[5]["settings"]["peers"][0]["endpoint"], "[2001:db8::9]:51820")
        tls = masked["inbounds"][2]["streamSettings"]["tlsSettings"]["certificates"][0]
        self.assertEqual(tls["key"], "****")                                    # a PEM key list: all of it
        self.assertEqual(tls["certificate"], ["-----BEGIN CERTIFICATE-----", "MIIBnotasecretcert"])
        self.assertEqual(masked["inbounds"][1]["streamSettings"]["realitySettings"]["shortIds"], ["", SID])
        self.assertEqual(masked["inbounds"][1]["settings"]["decryption"], "none")          # "none" is not secret
        self.assertEqual(masked["inbounds"][3]["settings"]["decryption"], "mlke" + ELL + "Wq")
        self.assertEqual(masked["inbounds"][0]["settings"]["accounts"][0]["user"], "owner")
        # addresses, ports, paths and tags are untouched
        self.assertEqual(out[1]["streamSettings"], config["outbounds"][1]["streamSettings"])
        self.assertEqual(mask_config({"outbounds": [{"settings": {"password": "", "id": None}}]}),
                         {"outbounds": [{"settings": {"password": "", "id": None}}]})

    def test_summarize_outbound_per_protocol(self):
        config = secret_config()
        self.assertEqual(summarize_outbound(config["outbounds"][0]), {
            "tag": "proxy", "protocol": "vless", "address": "203.0.113.10", "port": 443, "network": "tcp",
            "security": "reality", "sni": "www.microsoft.com", "host": "", "path": "", "flow": "xtls-rprx-vision",
            "id": "1111" + ELL + "55", "password": "", "method": "none", "user": ""})
        trojan = summarize_outbound(config["outbounds"][1])
        self.assertEqual({k: trojan[k] for k in ("protocol", "address", "port", "network", "security", "sni", "host",
                                                 "path", "password", "id")},
                         {"protocol": "trojan", "address": "tr.example.com", "port": 443, "network": "ws",
                          "security": "tls", "sni": "cdn.example.com", "host": "cdn.example.com", "path": "/tr",
                          "password": "Qx7K" + ELL + "23", "id": ""})
        ss = summarize_outbound(config["outbounds"][2])
        self.assertEqual((ss["method"], ss["password"], ss["network"], ss["security"], ss["port"]),
                         ("aes-256-gcm", "Sv8J" + ELL + "67", "tcp", "none", 8388))
        socks = summarize_outbound(config["outbounds"][3])
        self.assertEqual((socks["address"], socks["port"], socks["user"], socks["password"]),
                         ("10.0.0.2", 1080, "sockuser", "Sk5P" + ELL + "77"))
        wg = summarize_outbound(config["outbounds"][5])
        self.assertEqual((wg["address"], wg["port"], wg["network"], wg["password"]), ("2001:db8::9", 51820, "", ""))
        direct = summarize_outbound(config["outbounds"][-1])
        self.assertEqual((direct["protocol"], direct["address"], direct["port"], direct["network"]),
                         ("freedom", "", None, ""))
        for outbound in config["outbounds"]:
            self.assertNoSecret(json.dumps(summarize_outbound(outbound), ensure_ascii=False))

    def test_summarize_outbound_transports(self):
        def view(link):
            s = summarize_outbound(parse_share_link(link)[0])
            return s["network"], s["host"], s["path"]
        self.assertEqual(view(vless_link({"type": "grpc", "serviceName": "svc", "authority": "a.example.com"})),
                         ("grpc", "a.example.com", "svc"))
        self.assertEqual(view(vless_link({"type": "tcp", "headerType": "http", "host": "a.example.com,b.example.com",
                                          "path": "/p1,/p2"})), ("tcp", "a.example.com,b.example.com", "/p1,/p2"))
        self.assertEqual(view(vless_link({"type": "h2", "host": "a.example.com,b.example.com", "path": "/h"})),
                         ("h2", "a.example.com,b.example.com", "/h"))
        self.assertEqual(view(vless_link({"type": "httpupgrade", "host": "u.example.com", "path": "/u"})),
                         ("httpupgrade", "u.example.com", "/u"))
        self.assertEqual(view(vless_link({"type": "splithttp", "host": "s.example.com", "path": "/s"})),
                         ("splithttp", "s.example.com", "/s"))
        self.assertEqual(view(vless_link({"type": "ws", "path": "/w"})), ("ws", "", "/w"))
        short = summarize_outbound(parse_share_link(trojan_link(password="short-pw"))[0])
        self.assertEqual(short["password"], "****")
        # the flat vless form of newer xray and a newer ws "host" field
        flat = {"protocol": "vless", "settings": {"address": "f.example.com", "port": "8443", "id": UUID,
                                                  "encryption": "none"},
                "streamSettings": {"network": "ws", "wsSettings": {"host": "w.example.com", "path": "/f"}}}
        s = summarize_outbound(flat)
        self.assertEqual((s["address"], s["port"], s["id"], s["host"], s["tag"]),
                         ("f.example.com", 8443, "1111" + ELL + "55", "w.example.com", ""))

    def test_summarize_outbound_never_raises(self):
        for odd in (None, [], "x", 5, {}, {"protocol": 5}, {"protocol": "vless", "settings": []},
                    {"protocol": "vless", "settings": {"vnext": [None]}},
                    {"protocol": "vmess", "settings": {"vnext": [{"users": "x", "port": "abc"}]}},
                    {"protocol": "wireguard", "settings": {"peers": [{"endpoint": 5}]}},
                    {"protocol": "trojan", "settings": {"servers": [{"port": 99999, "password": 12345}]},
                     "streamSettings": {"network": 7, "security": ["x"]}}):
            with self.subTest(odd=odd):
                s = summarize_outbound(odd)
                self.assertEqual(len(s), 14)
                self.assertIsInstance(s["protocol"], str)
                self.assertIn(s["port"], (None,))

    def test_summarize_config(self):
        config = secret_config()
        config["inbounds"] += [{"tag": "open", "port": 1082, "protocol": "http"},
                               {"tag": "v6", "listen": "::1", "port": 1083, "protocol": "socks"},
                               {"tag": "unix", "listen": "/dev/shm/xray.sock", "port": 0, "protocol": "socks"},
                               {"tag": "range", "listen": "0.0.0.0", "port": "2000-2010", "protocol": "dokodemo-door"},
                               "not an object"]
        s = summarize_config(config)
        self.assertEqual(s["inbounds"], [
            {"tag": "http-in", "protocol": "http", "listen": "127.0.0.1", "port": 1081, "local_only": True},
            {"tag": "vless-in", "protocol": "vless", "listen": "0.0.0.0", "port": 8443, "local_only": False},
            {"tag": "trojan-in", "protocol": "trojan", "listen": "0.0.0.0", "port": 8444, "local_only": False},
            {"tag": "vless-enc-in", "protocol": "vless", "listen": "0.0.0.0", "port": 8445, "local_only": False},
            {"tag": "open", "protocol": "http", "listen": "0.0.0.0", "port": 1082, "local_only": False},
            {"tag": "v6", "protocol": "socks", "listen": "::1", "port": 1083, "local_only": True},
            {"tag": "unix", "protocol": "socks", "listen": "/dev/shm/xray.sock", "port": None, "local_only": True},
            {"tag": "range", "protocol": "dokodemo-door", "listen": "0.0.0.0", "port": "2000-2010",
             "local_only": False}])
        self.assertEqual(s["local_proxies"], ["http://127.0.0.1:1081", "http://127.0.0.1:1082", "socks5://[::1]:1083"])
        self.assertEqual(s["proxy_index"], 0)
        self.assertEqual(s["proxy"], summarize_outbound(config["outbounds"][0]))
        self.assertEqual(s["outbounds"][:2],
                         [{"tag": "proxy", "protocol": "vless"}, {"tag": "t", "protocol": "trojan"}])
        self.assertEqual(len(s["outbounds"]), len(config["outbounds"]))
        TestMasking.assertNoSecret(self, json.dumps(s, ensure_ascii=False))
        # the sample tunnel: both local proxies, the proxy outbound first
        s = summarize_config(sample_config())
        self.assertEqual(s["local_proxies"], ["http://127.0.0.1:1081", "socks5://127.0.0.1:1080"])
        self.assertEqual((s["proxy"]["protocol"], s["proxy"]["id"], s["proxy"]["host"]),
                         ("vmess", "6666" + ELL + "aa", ""))
        s = summarize_config({"outbounds": [{"tag": "direct", "protocol": "freedom"}]})
        self.assertEqual((s["proxy"], s["proxy_index"], s["inbounds"]), (None, None, []))
        for odd in (None, [], "x", {"inbounds": "x", "outbounds": 5}):
            self.assertEqual(summarize_config(odd), {"inbounds": [], "outbounds": [], "proxy": None,
                                                     "proxy_index": None, "local_proxies": []})


# --------------------------------------------------------------------------- xray -test

FAKE_XRAY = r'''
import os, sys, time
args = sys.argv[1:]
if len(args) != 4 or args[:3] != ["run", "-test", "-c"]:
    print("fake xray: unexpected arguments %r" % (args,))
    sys.exit(64)
path = args[3]
print("fake xray: mode %o" % (os.stat(path).st_mode & 0o777))
print("fake xray: args run -test -c PATH=" + path)
with open(path, encoding="utf-8") as f:
    text = f.read()
sys.stdout.flush()
if "FAKE_SLEEP" in text:
    time.sleep(60)
if "FAKE_ECHO" in text:
    print(text)
if "FAKE_LONG" in text:
    print("L" * 20000)
if "FAKE_BAD" in text:
    sys.stderr.write("Failed to start: main: failed to load config files: invalid outbound\n")
    sys.exit(1)
print("Configuration OK.")
'''

FAKE_OLD_XRAY = r'''
import sys
args = sys.argv[1:]
if args and args[0] == "run":
    sys.stderr.write("xray run: unknown command\nRun 'xray help' for usage.\n")
    sys.exit(2)
if len(args) == 3 and args[:2] == ["-test", "-config"]:
    print("fake old xray: args -test -config PATH=" + args[2])
    print("Configuration OK.")
    sys.exit(0)
print("fake old xray: unexpected arguments %r" % (args,))
sys.exit(64)
'''


class TestValidateWithXray(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bin_dir = tempfile.mkdtemp(prefix="vpn_fake_xray_")
        cls.xray = [sys.executable, cls.write_script("fake_xray.py", FAKE_XRAY)]
        cls.old_xray = [sys.executable, cls.write_script("fake_old_xray.py", FAKE_OLD_XRAY)]

    @classmethod
    def write_script(cls, name, text):
        path = os.path.join(cls.bin_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.bin_dir, ignore_errors=True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vpn_xray_tmp_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def validate(self, config, argv=None, **kw):
        ok, output = validate_with_xray(config, argv or self.xray, tmp_dir=self.tmp, **kw)
        self.assertEqual(os.listdir(self.tmp), [])            # the temporary config is always removed
        self.assertLessEqual(len(output), vpn.XRAY_OUTPUT_LIMIT)
        return ok, output

    def config_path(self, output):
        m = re.search(r"PATH=(.+)$", output, re.M)
        self.assertIsNotNone(m, output)
        return m.group(1).strip()

    def test_a_valid_config_passes(self):
        ok, output = self.validate(dumps_config(sample_config()))
        self.assertTrue(ok, output)
        self.assertIn("Configuration OK.", output)
        path = self.config_path(output)
        self.assertEqual(os.path.dirname(path), self.tmp)
        self.assertTrue(os.path.basename(path).startswith("xray-test-") and path.endswith(".json"), path)
        self.assertFalse(os.path.exists(path))
        if os.name == "posix":
            self.assertIn("fake xray: mode 600", output)       # private temporary file

    def test_an_invalid_config_fails_with_xrays_output(self):
        config = sample_config()
        config["remarks"] = "FAKE_BAD"
        ok, output = self.validate(dumps_config(config))
        self.assertFalse(ok)
        self.assertIn("failed to load config files: invalid outbound", output)

    def test_secrets_are_redacted_from_the_output(self):
        full = secret_config()
        config = {"remarks": "FAKE_ECHO", "inbounds": full["inbounds"], "outbounds": full["outbounds"][:3]}
        ok, output = self.validate(json.dumps(config))
        self.assertTrue(ok, output)
        self.assertNotIn("output shortened", output)           # the whole echo is there
        self.assertIn('"id": "6666' + ELL + 'aa"', output)     # UUID2, masked where xray printed it
        self.assertIn('"MIIE' + ELL + 'SC"', output)           # a PEM key line (a list value)
        self.assertIn('"Qx7K' + ELL + '23"', output)
        for secret in CONFIG_SECRETS[:8]:
            self.assertIn(secret, json.dumps(config))          # each secret was really in the echo
            self.assertNotIn(secret, output)
        config = secret_config()
        config["remarks"] = "FAKE_ECHO"
        ok, output = self.validate(dumps_config(config))
        for secret in CONFIG_SECRETS:
            self.assertNotIn(secret, output)
        # also when the text is not valid JSON (a failed check is usually about such a text)
        broken = '{"remarks": "FAKE_ECHO FAKE_BAD", "outbounds": [{"settings": {"password": "%s"' % TROJAN_PW
        ok, output = self.validate(broken)
        self.assertFalse(ok)
        self.assertNotIn(TROJAN_PW, output)
        self.assertIn("FAKE_ECHO", output)

    def test_an_old_xray_is_retried_with_the_old_syntax(self):
        ok, output = self.validate(dumps_config(sample_config()), argv=self.old_xray)
        self.assertTrue(ok, output)
        self.assertIn("args -test -config PATH=", output)
        self.assertIn("checked with '-test -config'", output)
        self.assertEqual(os.path.dirname(self.config_path(output)), self.tmp)

    def test_the_output_is_capped_and_keeps_the_end(self):
        ok, output = self.validate('{"remarks": "FAKE_LONG"}')
        self.assertTrue(ok)
        self.assertEqual(len(output), vpn.XRAY_OUTPUT_LIMIT)
        self.assertIn("output shortened", output)
        self.assertTrue(output.startswith("fake xray: mode"), output[:80])
        self.assertTrue(output.endswith("Configuration OK."), output[-80:])

    def test_a_hanging_xray_is_stopped(self):
        started = time.monotonic()
        ok, output = self.validate('{"remarks": "FAKE_SLEEP"}', timeout=1.5)
        self.assertFalse(ok)
        self.assertIn("did not finish within 1.5 seconds", output)
        self.assertLess(time.monotonic() - started, 30)

    def test_a_missing_binary_is_a_failure_not_an_exception(self):
        ok, output = self.validate("{}", argv=[os.path.join(self.bin_dir, "no-such-xray")])
        self.assertFalse(ok)
        self.assertTrue(output.startswith("cannot run no-such-xray: "), output)

    def test_argument_shapes(self):
        ok, output = self.validate(sample_config())                              # a dict is dumped first
        self.assertTrue(ok, output)
        ok, output = self.validate(dumps_config(sample_config()).encode("utf-8"))
        self.assertTrue(ok, output)
        seen = []

        def fake_run(argv, **kw):
            seen.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, b"Configuration OK.\r\n\x1b[32mdone\x1b[0m\n", None)

        with mock.patch.object(vpn.subprocess, "run", side_effect=fake_run):
            ok, output = self.validate("{}", argv="/opt/xray/xray")            # one path string is enough
        self.assertEqual((ok, output), (True, "Configuration OK.\ndone"))
        argv, kw = seen[0]
        self.assertEqual(argv[:4], ["/opt/xray/xray", "run", "-test", "-c"])
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertEqual(kw["stderr"], subprocess.STDOUT)
        self.assertEqual(kw["timeout"], 20)
        for bad in ([], "", [""], [5], None, ("xray", None)):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_with_xray("{}", bad, tmp_dir=self.tmp)
        with self.assertRaises(ValueError):
            validate_with_xray(12345, self.xray, tmp_dir=self.tmp)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_the_config_env_of_xray_is_not_inherited(self):
        seen = []

        def fake_run(argv, **kw):
            seen.append(kw["env"])
            return subprocess.CompletedProcess(argv, 0, b"ok", None)

        with mock.patch.dict(os.environ, {"XRAY_LOCATION_CONFDIR": "/etc/xray/conf.d"}), \
                mock.patch.object(vpn.subprocess, "run", side_effect=fake_run):
            self.validate("{}")
        self.assertNotIn("XRAY_LOCATION_CONFDIR", seen[0])
        self.assertIn("PATH", {k.upper() for k in seen[0]})


# --------------------------------------------------------------------------- proxy probe

def recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise OSError("closed")
        buf += chunk
    return buf


def drain(conn):
    while conn.recv(4096):
        pass


class FakeProxy(object):
    """A proxy double on 127.0.0.1 (socketserver in a thread). kind "http": answers `status` to CONNECT
    (then, with after="garbage", answers the TLS ClientHello with plain text); "socks5": no-auth SOCKS5
    answering `reply` (auth=True: asks for authentication); "silent": accepts and never answers."""

    def __init__(self, kind, status=b"200 Connection established", reply=0, auth=False, after=None):
        self.kind, self.status, self.reply, self.auth, self.after = kind, status, reply, auth, after
        self.requests = []
        fake = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(10)
                try:
                    fake.serve(self.request)
                except OSError:
                    pass

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = "%s://127.0.0.1:%d" % ("socks5" if kind == "socks5" else "http", self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05})
        self.thread.daemon = True
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def serve(self, conn):
        if self.kind == "silent":
            drain(conn)
        elif self.kind == "http":
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(1024)
                if not chunk:
                    return
                head += chunk
            self.requests.append(head.decode("latin-1"))
            conn.sendall(b"HTTP/1.1 " + self.status + b"\r\n\r\n")
            if self.after == "garbage":
                conn.recv(4096)                                  # the TLS ClientHello
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\nthis is not TLS\r\n")
                return
            drain(conn)
        else:
            _, n = recv_exact(conn, 2)
            methods = recv_exact(conn, n)
            if self.auth:
                conn.sendall(b"\x05\xff")
                self.requests.append(("auth?", methods))
                return
            conn.sendall(b"\x05\x00")
            _, command, _, kind = recv_exact(conn, 4)
            if kind == 1:
                address = socket.inet_ntoa(recv_exact(conn, 4))
            elif kind == 4:
                address = socket.inet_ntop(socket.AF_INET6, recv_exact(conn, 16))
            else:
                address = recv_exact(conn, recv_exact(conn, 1)[0]).decode("ascii")
            port = struct.unpack(">H", recv_exact(conn, 2))[0]
            self.requests.append((command, kind, address, port, methods))
            conn.sendall(b"\x05" + bytes([self.reply]) + b"\x00\x04" + b"\x00" * 16 + b"\x00\x00")
            drain(conn)


class TestProxyTest(unittest.TestCase):
    def proxy(self, *args, **kw):
        fake = FakeProxy(*args, **kw)
        self.addCleanup(fake.close)
        return fake

    def check(self, results, targets, ok=True):
        self.assertEqual([r["target"] for r in results], targets)
        for r in results:
            self.assertEqual(sorted(r), ["error", "ms", "ok", "target"])
            self.assertIsInstance(r["ms"], int)
            self.assertGreaterEqual(r["ms"], 0)
            self.assertEqual(r["ok"], ok, r)
            self.assertEqual(r["error"] == "", ok, r)
        return results

    def test_parse_proxy_url(self):
        for url, expected in (("http://127.0.0.1:1081", ("http", "127.0.0.1", 1081)),
                              ("HTTP://127.0.0.1:1081/", ("http", "127.0.0.1", 1081)),
                              ("  127.0.0.1:1081 ", ("http", "127.0.0.1", 1081)),
                              ("socks5://127.0.0.1:1080", ("socks5", "127.0.0.1", 1080)),
                              ("socks5h://localhost:1080", ("socks5", "localhost", 1080)),
                              ("http://[::1]:1081", ("http", "::1", 1081))):
            with self.subTest(url=url):
                self.assertEqual(parse_proxy_url(url), expected)
        for url, part in (("", "empty"), (None, "empty"), (1081, "empty"), ("ftp://127.0.0.1:21", "'ftp'"),
                          ("https://127.0.0.1:1081", "'https'"), ("socks4://127.0.0.1:1080", "'socks4'"),
                          ("http://127.0.0.1", "port is missing"), ("http://127.0.0.1:0", "between 1 and 65535"),
                          ("http://127.0.0.1:99999", "between 1 and 65535"), ("http://127.0.0.1:1081/path", "no path"),
                          ("http://127.0.0.1:1081?x=1", "no path"), ("http://::1:1081", "brackets"),
                          ("1://x:1", "invalid scheme"), ("http://bad host:1", "no path, query or spaces")):
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as cm:
                    parse_proxy_url(url)
                self.assertIn(part, str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            parse_proxy_url("http://owner:%s@127.0.0.1:1081" % TROJAN_PW)
        self.assertIn("user name or password are not supported", str(cm.exception))
        self.assertNotIn(TROJAN_PW, str(cm.exception))

    def test_http_connect_to_every_default_target(self):
        fake = self.proxy("http")
        results = self.check(proxy_test(fake.url, timeout=5, tls=False),
                             ["api.moonshot.ai:443", "api.telegram.org:443", "openrouter.ai:443"])
        self.assertEqual([r.split("\r\n")[0] for r in fake.requests],
                         ["CONNECT api.moonshot.ai:443 HTTP/1.1", "CONNECT api.telegram.org:443 HTTP/1.1",
                          "CONNECT openrouter.ai:443 HTTP/1.1"])
        self.assertIn("\r\nHost: api.moonshot.ai:443\r\n", fake.requests[0])
        self.assertLess(max(r["ms"] for r in results), 5000)

    def test_socks5_connect_by_name_and_by_address(self):
        fake = self.proxy("socks5")
        self.check(proxy_test(fake.url, targets=(("api.telegram.org", 443), ("203.0.113.5", 8443),
                                                 ("2001:db8::7", 443), "openrouter.ai"), timeout=5, tls=False),
                   ["api.telegram.org:443", "203.0.113.5:8443", "[2001:db8::7]:443", "openrouter.ai:443"])
        self.assertEqual(fake.requests, [(1, 3, "api.telegram.org", 443, b"\x00"), (1, 1, "203.0.113.5", 8443, b"\x00"),
                                         (1, 4, "2001:db8::7", 443, b"\x00"), (1, 3, "openrouter.ai", 443, b"\x00")])

    def test_ipv6_and_string_targets_through_http(self):
        fake = self.proxy("http")
        self.check(proxy_test(fake.url, targets=[("2001:db8::7", 443), "api.moonshot.ai:8443", "[2001:db8::8]:443"],
                              timeout=5, tls=False),
                   ["[2001:db8::7]:443", "api.moonshot.ai:8443", "[2001:db8::8]:443"])
        self.assertEqual([r.split("\r\n")[0] for r in fake.requests],
                         ["CONNECT [2001:db8::7]:443 HTTP/1.1", "CONNECT api.moonshot.ai:8443 HTTP/1.1",
                          "CONNECT [2001:db8::8]:443 HTTP/1.1"])
        results = proxy_test(fake.url, targets=("api.moonshot.ai", 443), timeout=5, tls=False)   # one pair
        self.check(results, ["api.moonshot.ai:443"])

    def test_a_proxy_that_answers_403(self):
        fake = self.proxy("http", status=b"403 Forbidden")
        results = self.check(proxy_test(fake.url, targets=[("api.moonshot.ai", 443)], timeout=5, tls=False),
                             ["api.moonshot.ai:443"], ok=False)
        self.assertEqual(results[0]["error"], "HTTP CONNECT: proxy answered 403 Forbidden")
        fake = self.proxy("http", status=b"502 Bad Gateway \x00\x07")
        results = proxy_test(fake.url, targets=[("api.moonshot.ai", 443)], timeout=5, tls=True)
        self.assertEqual(results[0]["error"], "HTTP CONNECT: proxy answered 502 Bad Gateway")   # no TLS tried

    def test_a_refused_port(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        results = self.check(proxy_test("http://127.0.0.1:%d" % port, targets=[("api.moonshot.ai", 443)],
                                        timeout=10, tls=False), ["api.moonshot.ai:443"], ok=False)
        self.assertEqual(results[0]["error"], "connect to proxy: connection refused")

    def test_a_silent_proxy_times_out(self):
        fake = self.proxy("silent")
        started = time.monotonic()
        results = self.check(proxy_test(fake.url, targets=[("api.moonshot.ai", 443)], timeout=0.5, tls=False),
                             ["api.moonshot.ai:443"], ok=False)
        self.assertEqual(results[0]["error"], "HTTP CONNECT: timed out")
        self.assertGreaterEqual(results[0]["ms"], 400)
        self.assertLess(time.monotonic() - started, 5)
        socks = FakeProxy("silent")
        self.addCleanup(socks.close)
        results = proxy_test("socks5://127.0.0.1:%d" % socks.port, targets=[("api.moonshot.ai", 443)], timeout=0.5)
        self.assertEqual(results[0]["error"], "SOCKS5: timed out")

    def test_socks5_errors(self):
        fake = self.proxy("socks5", reply=5)
        results = self.check(proxy_test(fake.url, targets=[("api.moonshot.ai", 443)], timeout=5, tls=False),
                             ["api.moonshot.ai:443"], ok=False)
        self.assertEqual(results[0]["error"], "SOCKS5: connection refused by the target")
        fake = self.proxy("socks5", reply=4)
        self.assertEqual(proxy_test(fake.url, targets=[("x.example.com", 443)], timeout=5)[0]["error"],
                         "SOCKS5: host unreachable")
        fake = self.proxy("socks5", auth=True)
        self.assertEqual(proxy_test(fake.url, targets=[("x.example.com", 443)], timeout=5)[0]["error"],
                         "SOCKS5: the SOCKS5 proxy requires authentication")
        http = self.proxy("http")                          # an HTTP proxy spoken to as SOCKS5, and vice versa
        result = proxy_test("socks5://127.0.0.1:%d" % http.port, targets=[("x.example.com", 443)], timeout=2)[0]
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"].startswith("SOCKS5: "), result)
        socks = self.proxy("socks5")
        result = proxy_test("http://127.0.0.1:%d" % socks.port, targets=[("x.example.com", 443)], timeout=2)[0]
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"].startswith("HTTP CONNECT: "), result)

    def test_the_tls_handshake_follows_the_connect(self):
        fake = self.proxy("http", after="garbage")
        results = self.check(proxy_test(fake.url, targets=[("api.moonshot.ai", 443)], timeout=5, tls=True),
                             ["api.moonshot.ai:443"], ok=False)
        self.assertTrue(results[0]["error"].startswith("TLS handshake: "), results)
        self.assertLessEqual(len(results[0]["error"]), 200)
        self.assertEqual(fake.requests[0].split("\r\n")[0], "CONNECT api.moonshot.ai:443 HTTP/1.1")

    def test_invalid_input_never_raises(self):
        fake = self.proxy("http")
        results = proxy_test(fake.url, targets=[("bad host\r\nX-Injected: 1", 443), "api.telegram.org:443",
                                                ("api.moonshot.ai", 0), 42, ("a", "b", "c")], timeout=5, tls=False)
        self.assertEqual([r["ok"] for r in results], [False, True, False, False, False])
        self.assertTrue(results[0]["error"].startswith("invalid target: "), results[0])
        self.assertIn("between 1 and 65535", results[2]["error"])
        self.assertTrue(results[3]["error"].startswith("invalid target"), results[3])
        self.assertEqual(len(fake.requests), 1)                    # only the valid target reached the proxy
        self.assertNotIn("X-Injected", fake.requests[0])
        for url in ("ftp://127.0.0.1:21", None, "http://owner:%s@127.0.0.1:1081" % TROJAN_PW):
            with self.subTest(url=url):
                results = proxy_test(url, timeout=1)
                self.check(results, ["api.moonshot.ai:443", "api.telegram.org:443", "openrouter.ai:443"], ok=False)
                self.assertTrue(all(r["error"].startswith("invalid proxy URL: ") for r in results))
                self.assertNotIn(TROJAN_PW, json.dumps(results))
        self.assertEqual(proxy_test(fake.url, targets=[], timeout=1), [])
        self.check(proxy_test(fake.url, targets=None, timeout="soon", tls=False),
                   ["api.moonshot.ai:443", "api.telegram.org:443", "openrouter.ai:443"])


# --------------------------------------------------------------------------- atomic write

class TestAtomicWriteText(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vpn_atomic_")
        self.path = os.path.join(self.dir, "config.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def read(self, path=None):
        with open(path or self.path, "rb") as f:
            return f.read()

    def test_a_new_file_is_written_byte_for_byte_and_private(self):
        text = '{\n  "remarks": "\u0633\u0631\u0648\u0631"\n}\n'
        self.assertEqual(atomic_write_text(self.path, text), os.path.realpath(self.path))
        self.assertEqual(self.read(), text.encode("utf-8"))                     # LF kept, UTF-8
        self.assertEqual(os.listdir(self.dir), ["config.json"])               # no temporary file left
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        atomic_write_text(self.path, b"\x00bytes\r\n")
        self.assertEqual(self.read(), b"\x00bytes\r\n")
        with self.assertRaises(TypeError):
            atomic_write_text(self.path, 5)

    def test_an_existing_file_keeps_its_mode_and_owner(self):
        with open(self.path, "w") as f:
            f.write("old")
        if os.name == "posix":
            os.chmod(self.path, 0o640)
        before = os.stat(self.path)
        with mock.patch.object(vpn.os, "chown", create=True) as chown:
            atomic_write_text(self.path, "new\n")
        self.assertEqual(self.read(), b"new\n")
        chown.assert_called_once()
        tmp, uid, gid = chown.call_args[0]
        self.assertEqual((uid, gid), (before.st_uid, before.st_gid))
        self.assertEqual(os.path.dirname(tmp), os.path.dirname(os.path.realpath(self.path)))
        self.assertTrue(os.path.basename(tmp).startswith(".config.json."), tmp)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)
            atomic_write_text(self.path, "newer\n", mode=0o644)                   # an explicit mode wins
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o644)
        self.assertEqual(os.listdir(self.dir), ["config.json"])

    def test_a_chown_that_is_not_allowed_is_tolerated(self):
        with open(self.path, "w") as f:
            f.write("old")
        with mock.patch.object(vpn.os, "chown", create=True, side_effect=PermissionError(1, "not permitted")):
            atomic_write_text(self.path, "new")
        self.assertEqual(self.read(), b"new")

    def test_a_new_file_does_not_try_to_chown(self):
        with mock.patch.object(vpn.os, "chown", create=True) as chown:
            atomic_write_text(self.path, "x", mode=0o644)
        chown.assert_not_called()
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o644)

    def test_a_failed_replace_keeps_the_old_file_and_leaves_no_temporary(self):
        with open(self.path, "w") as f:
            f.write("old")
        with mock.patch.object(vpn.os, "replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                atomic_write_text(self.path, "new")
        self.assertEqual(self.read(), b"old")
        self.assertEqual(os.listdir(self.dir), ["config.json"])

    def test_a_symlinked_config_keeps_its_link(self):
        real = os.path.join(self.dir, "real.json")
        with open(real, "w") as f:
            f.write("old")
        try:
            os.symlink(real, self.path)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("cannot create a symlink here")
        atomic_write_text(self.path, "new")
        self.assertTrue(os.path.islink(self.path))
        self.assertEqual(self.read(real), b"new")
        self.assertEqual(sorted(os.listdir(self.dir)), ["config.json", "real.json"])


if __name__ == "__main__":
    unittest.main()
