"""Secret-safe DingTalk delivery for a rendered Markdown report."""

from __future__ import annotations

import base64
from collections.abc import Callable
import hashlib
import hmac
import logging
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .report import RenderedReport


class NotificationError(RuntimeError):
    """Raised when DingTalk does not explicitly accept a notification."""


def suppress_secret_bearing_http_logs() -> None:
    """Prevent third-party request logs from exposing credential-bearing URLs."""
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class DingTalkNotifier:
    """Send exactly one DingTalk Markdown message per report."""

    def __init__(
        self,
        webhook: str,
        secret: str,
        client: httpx.Client | None = None,
        timeout_seconds: float = 15.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not secret:
            raise ValueError("DingTalk signing secret must not be empty")
        self._webhook = webhook
        self._secret = secret
        self._client = client or httpx.Client()
        self._timeout_seconds = timeout_seconds
        self._clock = clock

    def send(self, report: RenderedReport) -> None:
        suppress_secret_bearing_http_logs()
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": report.title, "text": report.markdown},
        }
        try:
            response = self._client.post(
                self._signed_webhook(),
                json=payload,
                timeout=self._timeout_seconds,
            )
        except (httpx.RequestError, ConnectionError, TimeoutError):
            raise NotificationError("DingTalk request failed") from None

        if not 200 <= response.status_code < 300:
            raise NotificationError("DingTalk HTTP request failed")
        try:
            result = response.json()
        except (ValueError, TypeError):
            raise NotificationError("DingTalk returned an invalid response") from None
        if not isinstance(result, dict) or "errcode" not in result:
            raise NotificationError("DingTalk returned an invalid response")
        errcode = result["errcode"]
        if not isinstance(errcode, int) or isinstance(errcode, bool):
            raise NotificationError("DingTalk returned an invalid response")
        if errcode != 0:
            raise NotificationError(f"DingTalk rejected message (errcode={errcode})")

    def _signed_webhook(self) -> str:
        timestamp = str(int(self._clock() * 1000))
        string_to_sign = f"{timestamp}\n{self._secret}"
        digest = hmac.new(
            self._secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        signature = base64.b64encode(digest).decode("ascii")

        parts = urlsplit(self._webhook)
        query = parse_qsl(parts.query, keep_blank_values=True)
        query.extend((("timestamp", timestamp), ("sign", signature)))
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )
