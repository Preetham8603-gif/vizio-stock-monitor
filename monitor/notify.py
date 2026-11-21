"""Telegram Bot API delivery with retries. Credentials come only from the
environment (GitHub Actions secrets); nothing is ever logged or committed."""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .fetch import _ssl_context

log = logging.getLogger("monitor.notify")


class NotifyError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str | None = None, chat_id: str | None = None, sender=None, sleep=time.sleep):
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID", "")
        self._send = sender or self._http_send
        self._sleep = sleep

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _http_send(self, payload: dict) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20, context=_ssl_context()) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode())
            except Exception:
                return {"ok": False, "error_code": e.code, "description": f"HTTP {e.code}"}

    def send(self, text: str, silent: bool = False, attempts: int = 4) -> int:
        """Send a message; returns Telegram message_id. Raises NotifyError."""
        if not self.configured:
            raise NotifyError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        payload = {"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": "false",
                   "disable_notification": "true" if silent else "false"}
        last = ""
        for i in range(1, attempts + 1):
            try:
                resp = self._send(payload)
            except Exception as e:  # network problems
                resp = {"ok": False, "description": f"{type(e).__name__}: {e}"}
            if resp.get("ok"):
                return int(resp["result"]["message_id"])
            last = str(resp.get("description") or resp)
            code = resp.get("error_code")
            if code in (400, 401, 403, 404):  # bad token / chat / bot blocked: retrying won't help
                break
            wait = (resp.get("parameters") or {}).get("retry_after") or 2 * (2 ** (i - 1))
            if i < attempts:
                log.warning("telegram send failed (%s); retrying in %ss", last, wait)
                self._sleep(min(float(wait), 30))
        # Never echo the token in errors.
        raise NotifyError(last.replace(self.token, "***") if self.token else last)
