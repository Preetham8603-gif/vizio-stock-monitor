"""Polite HTTP fetching: one GET per retailer per cycle, timeouts, bounded
exponential backoff, and no retries against an explicit block (403/429)."""
from __future__ import annotations

import email.utils
import gzip
import logging
import random
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("monitor.fetch")

# A normal desktop browser request. We do not solve challenges, rotate IPs,
# or spoof cookies; if a retailer challenges us we report BLOCKED.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


@dataclass
class Fetched:
    url: str
    status: int | None
    body: str
    error: str = ""
    attempts: int = 0
    elapsed_s: float = 0.0
    stale_seconds: float | None = None   # server Date vs. our clock


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # optional, helps on macOS python.org builds
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def fetch(url: str, timeout: float = 25.0, retries: int = 3, base_delay: float = 3.0, sleep=time.sleep) -> Fetched:
    ctx = _ssl_context()
    started = time.monotonic()
    last_err, last_status = "", None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                body = raw.decode("utf-8", errors="replace")
                stale = None
                date_hdr = resp.headers.get("Date")
                if date_hdr:
                    try:
                        server_now = email.utils.parsedate_to_datetime(date_hdr)
                        stale = (datetime.now(timezone.utc) - server_now).total_seconds()
                    except Exception:
                        pass
                age = resp.headers.get("Age")
                if age and age.isdigit():
                    stale = max(stale or 0, float(age))
                return Fetched(url, resp.status, body, attempts=attempt, elapsed_s=time.monotonic() - started, stale_seconds=stale)
        except urllib.error.HTTPError as e:
            last_status = e.code
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            last_err = f"HTTP {e.code}"
            if e.code in (403, 412, 429):
                # Explicit refusal: do not hammer. Report it as-is.
                return Fetched(url, e.code, body, last_err, attempt, time.monotonic() - started)
            if e.code < 500:
                return Fetched(url, e.code, body, last_err, attempt, time.monotonic() - started)
        except Exception as e:  # timeouts, DNS, resets
            last_err = f"{type(e).__name__}: {e}"
        if attempt < retries:
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1.5)
            log.warning("fetch %s failed (%s); retry %d/%d in %.1fs", url, last_err, attempt, retries - 1, delay)
            sleep(delay)
    return Fetched(url, last_status, "", last_err or "unknown error", retries, time.monotonic() - started)
