"""Public GET-only transport. No account client, credentials or proxy environment."""
import http.client
import json
import re
import time
import urllib.error
import urllib.request
from datetime import timezone
from email.utils import parsedate_to_datetime

from .flow_features import symbol_checked

BITPIN = "https://api.bitpin.org"
STABLECOINS = "https://stablecoins.llama.fi/stablecoins?includePrices=true"
PUBLIC_URL = re.compile(r"https://api[.]bitpin[.]org/api/v1/mth/(matches|orderbook)/[A-Z0-9]{2,16}_(IRT|USDT)/")


class PublicDataError(Exception):
    def __init__(self, message, retry_after=0, status=None):
        super().__init__(message)
        self.retry_after = retry_after
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PublicDataError("public source redirect refused")


def retry_seconds(header):
    try:
        seconds = float(header)
    except (ValueError, TypeError):
        try:
            stamp = parsedate_to_datetime(header)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            seconds = stamp.timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            seconds = 60
    if not 0 <= seconds <= 86400:
        seconds = 86400 if seconds > 0 else 60
    return max(30, seconds)


class PublicFlowClient:
    def __init__(self, opener=None, timeout=10, max_bytes=8 * 1024 * 1024):
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.timeout = timeout
        self.max_bytes = max_bytes

    def _allowed_url(self, url):
        return url == STABLECOINS or PUBLIC_URL.fullmatch(url)

    def get_json(self, url):
        if not self._allowed_url(url):
            raise ValueError("only approved public data endpoints are allowed")
        req = urllib.request.Request(url, headers={"User-Agent": "BitpinFlowShadow/1.0",
                                                  "Accept": "application/json"}, method="GET")
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                raw = response.read(self.max_bytes + 1)
                if len(raw) > self.max_bytes:
                    raise PublicDataError("public response exceeds size limit")
        except urllib.error.HTTPError as exc:
            delay = retry_seconds(exc.headers.get("Retry-After")) if exc.code in (429, 503) else 60
            status = exc.code
            exc.close()
            raise PublicDataError("public source HTTP %d" % status, delay, status=status)
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise PublicDataError("public source network error: " + type(exc).__name__, 60)
        try:
            def invalid_constant(value):
                raise ValueError("nonfinite JSON number")
            return json.loads(raw.decode("utf-8"), parse_constant=invalid_constant)
        except (ValueError, UnicodeError):
            raise PublicDataError("invalid public JSON", 60)

    def matches(self, symbol):
        return self.get_json(BITPIN + "/api/v1/mth/matches/" + symbol_checked(symbol) + "/")

    def orderbook(self, symbol):
        return self.get_json(BITPIN + "/api/v1/mth/orderbook/" + symbol_checked(symbol) + "/")

    def stablecoins(self):
        return self.get_json(STABLECOINS)
