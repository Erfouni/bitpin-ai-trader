# -*- coding: utf-8 -*-
"""bitpin.panel_auth: password hashes and rules, RFC 6238 TOTP, the login limiter and the session store.
No network, no real credentials."""
import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bitpin import panel_auth as pa  # noqa: E402

RFC_SECRET = base64.b32encode(b"12345678901234567890").decode("ascii")     # RFC 6238 appendix B, SHA1
RFC_VECTORS = ((59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"), (1234567890, "89005924"),
               (2000000000, "69279037"), (20000000000, "65353130"))
FAST = 1000          # iterations for tests that hash often (the format keeps the count; verify reads it)


class TestPasswordHash(unittest.TestCase):
    def test_default_format_is_pbkdf2_sha256_600000_with_a_16_byte_salt(self):
        h = pa.hash_password("correct horse battery staple")
        scheme, iters, salt, dk = h.split("$")
        self.assertEqual((scheme, iters), ("pbkdf2_sha256", "600000"))
        self.assertEqual(len(base64.b64decode(salt)), 16)
        self.assertEqual(len(base64.b64decode(dk)), 32)
        self.assertTrue(pa.verify_password("correct horse battery staple", h))
        self.assertFalse(pa.verify_password("correct horse battery stapl", h))
        self.assertEqual(pa.PBKDF2_ITERATIONS, 600000)

    def test_the_derived_key_is_standard_pbkdf2_hmac_sha256(self):
        # the widely published vector PBKDF2-HMAC-SHA256("password", "salt", 4096, 32) = c5e478d5...
        self.assertEqual(pa.hash_password("password", iterations=4096, salt=b"salt"),
                         "pbkdf2_sha256$4096$c2FsdA==$xeR41ZKIyEGqUw22hFxMjZYok6ABzk4RpJY4c6qYE0o=")
        self.assertEqual(base64.b64decode("xeR41ZKIyEGqUw22hFxMjZYok6ABzk4RpJY4c6qYE0o=").hex(),
                         "c5e478d59288c841aa530db6845c4c8d962893a001ce4e11a4963873aa98134a")

    def test_every_hash_has_its_own_salt(self):
        a, b = pa.hash_password("same password!", FAST), pa.hash_password("same password!", FAST)
        self.assertNotEqual(a, b)
        self.assertTrue(pa.verify_password("same password!", a) and pa.verify_password("same password!", b))

    def test_base64_without_padding_and_unicode_passwords(self):
        h = pa.hash_password(u"رمز-عبور-Ab1!", FAST)
        self.assertTrue(pa.verify_password(u"رمز-عبور-Ab1!", h))
        s, it, salt, dk = h.split("$")
        self.assertTrue(pa.verify_password(u"رمز-عبور-Ab1!", "$".join([s, it, salt.rstrip("="), dk.rstrip("=")])))

    def test_unknown_or_broken_formats_verify_false_and_never_raise(self):
        good = pa.hash_password("pw-for-format-tests", FAST)
        s, it, salt, dk = good.split("$")
        bad = [None, 123, b"bytes", "", "plain", "md5$1000$%s$%s" % (salt, dk), "pbkdf2_sha256$1000$%s" % salt,
               "pbkdf2_sha256$abc$%s$%s" % (salt, dk), "pbkdf2_sha256$999$%s$%s" % (salt, dk),
               "pbkdf2_sha256$99999999$%s$%s" % (salt, dk), "pbkdf2_sha256$1000$!!!!$%s" % dk,
               "pbkdf2_sha256$1000$%s$%s" % (salt, base64.b64encode(b"short").decode()),
               "pbkdf2_sha256$1000$%s$%s" % (base64.b64encode(b"tiny").decode(), dk),
               u"pbkdf2_sha256$١٠٠٠$%s$%s" % (salt, dk), good + "$extra", good.upper()]
        for stored in bad:
            self.assertFalse(pa.verify_password("pw-for-format-tests", stored), stored)
            self.assertFalse(pa.is_password_hash(stored), stored)
        self.assertTrue(pa.is_password_hash(good))
        self.assertFalse(pa.verify_password(None, good))
        self.assertFalse(pa.verify_password(b"pw-for-format-tests", good))
        self.assertFalse(pa.verify_password("x" * 2000, good))
        self.assertFalse(pa.verify_password(u"\udcff", good))                       # a lone surrogate

    def test_hash_refuses_bad_input(self):
        with self.assertRaises(TypeError):
            pa.hash_password(b"bytes")
        with self.assertRaises(ValueError):
            pa.hash_password("pw", iterations=10)


class TestPasswordRules(unittest.TestCase):
    def test_a_good_password_has_no_problems(self):
        self.assertEqual(pa.password_problems("Blue-Harbor-Tiger-42", "owner"), [])
        self.assertEqual(pa.password_problems(u"رمزِ امنِ من ۱۴۰۵ Az", "owner"), [])   # Persian letters count as "other"

    def test_length(self):
        p = pa.password_problems("Ab1!xyz", "owner")
        self.assertTrue(any(u"۱۲" in x or "12" in x for x in p), p)
        self.assertEqual(pa.password_problems("Tq7!mZp2#Lw9", "owner"), [])            # exactly 12

    def test_at_least_three_character_classes(self):
        self.assertTrue(pa.password_problems("abcdefghijklmn", "owner"))
        self.assertTrue(pa.password_problems("harborlight4271", "owner"))              # lower + digit = 2
        self.assertEqual(pa.password_problems("harborlight42!", "owner"), [])            # lower + digit + symbol
        self.assertEqual(pa.password_problems("HARBORlight4271", "owner"), [])           # upper + lower + digit
        self.assertTrue(pa.password_problems("abcdefgh1234!", "owner"))                  # a sequence: "common"

    def test_must_not_contain_the_username(self):
        self.assertTrue(pa.password_problems("My-OWNER-pass-99", "owner"))
        self.assertTrue(pa.password_problems("xxOwnerxx-2026!", "OWNER"))
        self.assertEqual(pa.password_problems("Blue-Harbor-Tiger-42", "ab"), [])         # a 1-2 letter name: no rule

    def test_common_passwords_are_refused(self):
        for pw in ("Password1234", "password123456", "qwerty123456", "Qwerty!23456789", "Password123!",
                   "Bitpin@2026!!", "1q2w3e4r5t6y", "Administrator1!", "iloveyou1234"):
            probs = pa.password_problems(pw, "owner")
            self.assertTrue(probs, pw)

    def test_repetitive_passwords_are_refused(self):
        self.assertTrue(pa.password_problems("Aa1!Aa1!Aa1!Aa1!", "owner"))

    def test_problems_are_persian_texts(self):
        probs = pa.password_problems("abc", "owner")
        self.assertTrue(probs)
        for p in probs:
            self.assertIsInstance(p, str)
            self.assertTrue(any(u"؀" <= ch <= u"ۿ" for ch in p), p)
        self.assertTrue(pa.password_problems(None, "owner"))
        self.assertTrue(pa.password_problems("", "owner"))


class TestTotp(unittest.TestCase):
    def test_rfc6238_sha1_vectors(self):
        for t, code8 in RFC_VECTORS:
            self.assertEqual(pa.totp(RFC_SECRET, t, digits=8), code8, t)
            self.assertEqual(pa.totp(RFC_SECRET, t), code8[-6:], t)                  # the panel's 6 digits
            ok, counter = pa.verify_totp(RFC_SECRET, code8[-6:], t)
            self.assertTrue(ok, t)
            self.assertEqual(counter, int(t // 30))

    def test_new_secret_is_20_random_bytes_in_base32(self):
        a, b = pa.new_totp_secret(), pa.new_totp_secret()
        self.assertNotEqual(a, b)
        self.assertEqual(len(a), 32)
        self.assertEqual(len(base64.b32decode(a)), 20)
        self.assertTrue(set(a) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"))

    def test_previous_current_and_next_step_are_accepted_nothing_further(self):
        t = 1111111111
        step = int(t // 30)
        for dt, expect in ((-30, step - 1), (0, step), (30, step + 1)):
            ok, counter = pa.verify_totp(RFC_SECRET, pa.totp(RFC_SECRET, t + dt), t)
            self.assertTrue(ok, dt)
            self.assertEqual(counter, expect)
        for dt in (-90, -60, 60, 90):
            self.assertEqual(pa.verify_totp(RFC_SECRET, pa.totp(RFC_SECRET, t + dt), t), (False, None), dt)

    def test_a_used_code_is_refused_again(self):
        t = 1234567890
        code = pa.totp(RFC_SECRET, t)
        ok, used = pa.verify_totp(RFC_SECRET, code, t)
        self.assertTrue(ok)
        self.assertEqual(pa.verify_totp(RFC_SECRET, code, t, last_counter=used), (False, None))
        self.assertEqual(pa.verify_totp(RFC_SECRET, code, t + 20, last_counter=used), (False, None))
        # the previous step is older than the one used: refused too
        self.assertEqual(pa.verify_totp(RFC_SECRET, pa.totp(RFC_SECRET, t - 30), t, last_counter=used), (False, None))
        # the next code is fine
        ok, nxt = pa.verify_totp(RFC_SECRET, pa.totp(RFC_SECRET, t + 30), t + 30, last_counter=used)
        self.assertTrue(ok)
        self.assertEqual(nxt, used + 1)

    def test_code_formats(self):
        t = 1111111111
        code = pa.totp(RFC_SECRET, t)                                                 # "050471"
        fa = code.translate(dict((ord(str(i)), c) for i, c in enumerate(u"۰۱۲۳۴۵۶۷۸۹")))
        ar = code.translate(dict((ord(str(i)), c) for i, c in enumerate(u"٠١٢٣٤٥٦٧٨٩")))
        for good in (code, " %s " % code, code[:3] + " " + code[3:], fa, ar):
            self.assertTrue(pa.verify_totp(RFC_SECRET, good, t)[0], repr(good))
        for bad in (code[:5], code + "1", "abcdef", "", None, 50471, u"٥٠٤٧١x", "0x0471"):
            self.assertEqual(pa.verify_totp(RFC_SECRET, bad, t), (False, None), repr(bad))

    def test_bad_secrets_never_raise(self):
        for s in ("not base32!", "", None, 123, "A" * 300):
            self.assertEqual(pa.verify_totp(s, "123456", 1111111111), (False, None), s)

    def test_secret_spacing_and_case_do_not_matter(self):
        t = 59
        spaced = " ".join(RFC_SECRET[i:i + 4] for i in range(0, 32, 4)).lower()
        self.assertEqual(pa.totp(spaced, t), pa.totp(RFC_SECRET, t))

    def test_otpauth_uri(self):
        uri = pa.otpauth_uri("JBSWY3DPEHPK3PXP", "owner name")
        self.assertTrue(uri.startswith("otpauth://totp/bitpin-bot:owner%20name?secret=JBSWY3DPEHPK3PXP"), uri)
        for part in ("issuer=bitpin-bot", "algorithm=SHA1", "digits=6", "period=30"):
            self.assertIn(part, uri)
        self.assertIn("otpauth://totp/my%20bot:u?", pa.otpauth_uri("JBSWY3DPEHPK3PXP", "u", issuer="my bot"))


class TestLoginLimiter(unittest.TestCase):
    T = 1790000000.0

    def test_five_failures_in_15_minutes_lock_the_ip_for_15_minutes(self):
        lim = pa.LoginLimiter()
        ip = "198.51.100.7"
        for i in range(4):
            self.assertEqual(lim.failure(ip, self.T + i * 60), [])
            self.assertEqual(lim.check(ip, self.T + i * 60 + 1), (True, ""))
        self.assertEqual(lim.failure(ip, self.T + 240), ["ip"])
        self.assertEqual(lim.check(ip, self.T + 241), (False, "ip"))
        self.assertEqual(lim.check("198.51.100.8", self.T + 241), (True, ""))       # another IP is not locked
        self.assertEqual(lim.check(ip, self.T + 240 + 899), (False, "ip"))
        self.assertEqual(lim.locked_until(ip, self.T + 300), self.T + 240 + 900)
        self.assertEqual(lim.check(ip, self.T + 240 + 900), (True, ""))            # unlocked after 15 minutes
        self.assertEqual(lim.failure(ip, self.T + 240 + 901), [])                   # and the count starts again

    def test_failures_spread_over_more_than_15_minutes_do_not_lock(self):
        lim = pa.LoginLimiter()
        ip = "198.51.100.9"
        for t in (0, 300, 600, 890, 1000, 1300):                                     # never 5 within 900 s
            self.assertEqual(lim.failure(ip, self.T + t), [], t)
        self.assertEqual(lim.check(ip, self.T + 1301), (True, ""))

    def test_success_forgets_the_ip_failures(self):
        lim = pa.LoginLimiter()
        ip = "203.0.113.4"
        for i in range(4):
            lim.failure(ip, self.T + i)
        lim.success(ip, self.T + 5)
        for i in range(4):
            self.assertEqual(lim.failure(ip, self.T + 10 + i), [])
        self.assertTrue(lim.check(ip, self.T + 20)[0])

    def test_thirty_failures_of_all_ips_in_an_hour_lock_every_login_for_an_hour(self):
        lim = pa.LoginLimiter()
        for i in range(29):
            self.assertEqual(lim.failure("10.0.%d.1" % i, self.T + i * 100), [])
        self.assertEqual(lim.failure("10.0.99.1", self.T + 2900), ["global"])
        self.assertEqual(lim.check("192.0.2.200", self.T + 2901), (False, "global"))
        self.assertEqual(lim.check("192.0.2.200", self.T + 2900 + 3599), (False, "global"))
        self.assertEqual(lim.check("192.0.2.200", self.T + 2900 + 3600), (True, ""))

    def test_the_global_count_is_per_hour(self):
        lim = pa.LoginLimiter()
        for i in range(29):
            lim.failure("10.1.%d.1" % i, self.T + i)
        self.assertEqual(lim.failure("10.1.200.1", self.T + 3700), [])              # the first 29 are older than 1 h
        self.assertTrue(lim.check("10.1.201.1", self.T + 3701)[0])

    def test_ipv6_counts_per_64_and_mapped_ipv4_as_ipv4(self):
        lim = pa.LoginLimiter()
        for i in range(5):
            started = lim.failure("2001:db8:1:2::%x" % (i + 1), self.T + i)
        self.assertEqual(started, ["ip"])
        self.assertEqual(lim.check("2001:db8:1:2:ffff::1", self.T + 10), (False, "ip"))
        self.assertTrue(lim.check("2001:db8:1:3::1", self.T + 10)[0])
        self.assertEqual(pa.ip_key("::ffff:192.0.2.1"), "192.0.2.1")
        self.assertEqual(pa.ip_key("2001:db8:1:2::1"), "2001:db8:1:2::/64")
        self.assertEqual(pa.ip_key("not an ip"), "not an ip")

    def test_a_lock_starts_only_once(self):
        lim = pa.LoginLimiter()
        ip = "192.0.2.50"
        starts = [lim.failure(ip, self.T + i) for i in range(8)]
        self.assertEqual(sum(1 for s in starts if "ip" in s), 1)


class TestTrustedBrowsers(unittest.TestCase):
    """Security review F1: a guessing flood from many addresses must not lock the owner out."""
    T = 1790000000.0

    def test_the_global_lock_does_not_apply_to_a_trusted_browser(self):
        lim = pa.LoginLimiter()
        for i in range(30):
            lim.failure("10.9.%d.1" % i, self.T + i)
        self.assertEqual(lim.check("192.0.2.9", self.T + 40), (False, "global"))
        self.assertEqual(lim.check("192.0.2.9", self.T + 40, trusted=True), (True, ""))
        self.assertEqual(lim.locked_until("192.0.2.9", self.T + 40, trusted=True), 0.0)
        for i in range(5):                                           # the per-IP lock still applies to it
            lim.failure("192.0.2.9", self.T + 50 + i)
        self.assertEqual(lim.check("192.0.2.9", self.T + 60, trusted=True), (False, "ip"))

    def test_device_cookies(self):
        dt = pa.DeviceTrust(b"k" * 32)
        tok = dt.issue("hash-1", self.T)
        dev = dt.check(tok, "hash-1", self.T + 10)
        self.assertTrue(dev)
        self.assertRegex(tok, r"^[A-Za-z0-9_-]{16,64}\.\d{9,12}\.[0-9a-f]{64}$")
        self.assertIsNone(dt.check(tok, "hash-2", self.T + 10))                  # a new password: all invalid
        self.assertIsNone(dt.check(tok, "hash-1", self.T + pa.DEVICE_MAX_AGE + 1))  # expired
        self.assertIsNone(dt.check(tok, "hash-1", self.T - 10))                  # from the future
        self.assertIsNone(dt.check(tok[:-1] + ("0" if tok[-1] != "0" else "1"), "hash-1", self.T))
        self.assertIsNone(pa.DeviceTrust(b"j" * 32).check(tok, "hash-1", self.T))   # another server secret
        for bad in ("", None, 5, "a.b.c", tok + "x", tok.replace(".", "..", 1)):
            self.assertIsNone(dt.check(bad, "hash-1", self.T), bad)
        for _ in range(4):
            dt.failure(dev)
        self.assertEqual(dt.check(tok, "hash-1", self.T), dev)
        dt.success(dev)                                                          # a success forgets them
        for _ in range(4):
            dt.failure(dev)
        self.assertEqual(dt.check(tok, "hash-1", self.T), dev)
        dt.failure(dev)                                                          # the 5th in a row
        self.assertIsNone(dt.check(tok, "hash-1", self.T))

    def test_the_device_secret_is_kept_private_and_reused(self):
        import tempfile
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "device_key")
            a = pa.load_device_secret(p)
            self.assertEqual(len(a), 32)
            self.assertEqual(pa.load_device_secret(p), a)
            if os.name == "posix":
                self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
            self.assertEqual(len(pa.load_device_secret(os.path.join(d, "missing-dir", "k"))), 32)
        finally:
            import shutil
            shutil.rmtree(d, True)


class TestSessions(unittest.TestCase):
    T = 1790000000.0

    def test_create_and_get_touches_last_seen(self):
        st = pa.SessionStore(30, 12)
        sid = st.create("owner", "192.0.2.1", self.T)
        self.assertGreaterEqual(len(sid), 40)
        s = st.get(sid, self.T + 60)
        self.assertEqual((s["user"], s["ip"], s["created"], s["last_seen"]), ("owner", "192.0.2.1", self.T, self.T + 60))
        self.assertGreaterEqual(len(s["csrf"]), 40)
        sid2 = st.create("owner", "192.0.2.1", self.T)
        self.assertNotEqual(sid, sid2)
        self.assertNotEqual(st.get(sid2, self.T)["csrf"], s["csrf"])               # a CSRF token per session

    def test_idle_expiry(self):
        st = pa.SessionStore(30, 12)
        sid = st.create("owner", "ip", self.T)
        self.assertIsNotNone(st.get(sid, self.T + 29 * 60))
        self.assertIsNotNone(st.get(sid, self.T + 58 * 60))                         # touched at 29 min
        self.assertIsNone(st.get(sid, self.T + 58 * 60 + 30 * 60 + 1))
        self.assertIsNone(st.get(sid, self.T + 58 * 60 + 60))                       # gone for good
        self.assertEqual(len(st), 0)

    def test_absolute_expiry(self):
        st = pa.SessionStore(30, 12)
        sid = st.create("owner", "ip", self.T)
        t = self.T
        while t < self.T + 12 * 3600 - 600:
            t += 600
            self.assertIsNotNone(st.get(sid, t), t - self.T)
        self.assertIsNone(st.get(sid, self.T + 12 * 3600 + 1))

    def test_destroy_and_destroy_all(self):
        st = pa.SessionStore()
        a, b = st.create("owner", "ip", self.T), st.create("owner", "ip", self.T)
        st.destroy(a)
        self.assertIsNone(st.get(a, self.T))
        self.assertIsNotNone(st.get(b, self.T))
        st.destroy_all()
        self.assertIsNone(st.get(b, self.T))
        st.destroy(None)
        st.destroy("")

    def test_unknown_and_malformed_ids(self):
        st = pa.SessionStore()
        st.create("owner", "ip", self.T)
        for sid in (None, "", "nope", "x" * 500, 12):
            self.assertIsNone(st.get(sid, self.T))

    def test_only_a_hash_of_the_sid_is_kept(self):
        st = pa.SessionStore()
        sid = st.create("owner", "ip", self.T)
        self.assertNotIn(sid, st._sessions)
        self.assertNotIn(sid, repr(st._sessions))

    def test_the_oldest_session_goes_when_the_store_is_full(self):
        st = pa.SessionStore(max_sessions=3)
        sids = [st.create("owner", "ip", self.T + i) for i in range(4)]
        self.assertIsNone(st.get(sids[0], self.T + 5))
        self.assertTrue(all(st.get(s, self.T + 5) for s in sids[1:]))


if __name__ == "__main__":
    unittest.main()
