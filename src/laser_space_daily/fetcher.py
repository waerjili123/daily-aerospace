"""Safe, bounded web fetching and deterministic article text extraction."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
import hashlib
import ipaddress
import socket
from typing import Any
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import httpx
from pydantic import Field
import trafilatura

from .models import Candidate, DomainModel


BEIJING = ZoneInfo("Asia/Shanghai")
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class FetchError(RuntimeError):
    """Base class for controlled fetch failures."""


class UnsafeUrl(FetchError):
    """Raised when a URL could reach a non-public network address."""


class PageTooLarge(FetchError):
    """Raised when the decoded response stream exceeds the configured bound."""


class FetchRedirectLimit(FetchError):
    """Raised when a response exceeds the configured redirect count."""


class FetchedPage(DomainModel):
    requested_url: str
    final_url: str
    status_code: int = Field(ge=100, le=599)
    title: str
    text: str
    fetched_at: datetime
    content_hash: str


Resolver = Callable[..., Iterable[Any]]


class PageFetcher:
    """Fetch pages with SSRF, redirect, body-size and timeout controls."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver | None = None,
        timeout: float = 15.0,
        max_bytes: int = 10 * 1024 * 1024,
        max_redirects: int = 5,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if max_redirects < 0:
            raise ValueError("max_redirects cannot be negative")
        self._transport = transport
        self._resolver = resolver or self._default_resolver
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects

    def fetch(self, candidate: Candidate) -> FetchedPage:
        requested_url = candidate.url
        current_url = requested_url
        redirects = 0

        with httpx.Client(
            transport=self._transport,
            timeout=self._timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            while True:
                parsed, addresses = self._validate_public_url(current_url)
                target_url, headers, extensions = self._connection_request(
                    parsed, addresses[0]
                )
                request = client.build_request(
                    "GET", target_url, headers=headers, extensions=extensions
                )
                response = client.send(request, stream=True)
                try:
                    if response.status_code in REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            return self._to_page(requested_url, current_url, response)
                        if redirects >= self._max_redirects:
                            raise FetchRedirectLimit(
                                f"redirect limit exceeded for {requested_url}"
                            )
                        next_url = urljoin(current_url, location)
                        self._validate_public_url(next_url)
                        current_url = next_url
                        redirects += 1
                        continue
                    return self._to_page(requested_url, current_url, response)
                finally:
                    response.close()

    def _validate_public_url(self, url: str) -> tuple[SplitResult, list[str]]:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise UnsafeUrl(f"only public http/https URLs are allowed: {url}")

        hostname = parsed.hostname.rstrip(".")
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            literal = None
        if literal is not None:
            self._reject_non_global(literal, url)

        try:
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        except ValueError as exc:
            raise UnsafeUrl(f"URL contains an invalid port: {url}") from exc
        try:
            resolved = list(self._resolver(hostname))
        except TypeError:
            resolved = list(self._resolver(hostname, port))
        except (OSError, ValueError) as exc:
            raise UnsafeUrl(f"unable to resolve URL host: {url}") from exc
        if not resolved:
            raise UnsafeUrl(f"URL host resolved to no addresses: {url}")

        addresses: list[str] = []
        for item in resolved:
            try:
                address = ipaddress.ip_address(self._address_text(item))
            except ValueError as exc:
                raise UnsafeUrl(f"resolver returned an invalid address for {url}") from exc
            self._reject_non_global(address, url)
            address_text = str(address)
            if address_text not in addresses:
                addresses.append(address_text)
        return parsed, addresses

    @staticmethod
    def _connection_request(
        parsed: SplitResult, address: str
    ) -> tuple[str, dict[str, str], dict[str, str]]:
        ip = ipaddress.ip_address(address)
        target_host = f"[{ip}]" if ip.version == 6 else str(ip)
        source_host = parsed.hostname.rstrip(".")
        host_header = f"[{source_host}]" if ":" in source_host else source_host
        if parsed.port is not None:
            target_host = f"{target_host}:{parsed.port}"
            host_header = f"{host_header}:{parsed.port}"
        target_url = urlunsplit(
            (parsed.scheme, target_host, parsed.path, parsed.query, "")
        )
        return (
            target_url,
            {"Host": host_header, "Connection": "close"},
            {"sni_hostname": source_host},
        )

    @staticmethod
    def _default_resolver(hostname: str) -> Iterable[Any]:
        return socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)

    @staticmethod
    def _address_text(item: Any) -> str:
        if isinstance(item, (str, ipaddress.IPv4Address, ipaddress.IPv6Address)):
            return str(item)
        if isinstance(item, tuple):
            if len(item) >= 5 and isinstance(item[4], tuple):
                return str(item[4][0])
            if item:
                return str(item[0])
        return str(item)

    @staticmethod
    def _reject_non_global(address: ipaddress.IPv4Address | ipaddress.IPv6Address, url: str) -> None:
        if (
            not address.is_global
            or address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise UnsafeUrl(f"URL resolves to a non-public address: {url}")

    def _to_page(
        self, requested_url: str, final_url: str, response: httpx.Response
    ) -> FetchedPage:
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self._max_bytes:
                    raise PageTooLarge(f"response exceeds {self._max_bytes} bytes")
            except ValueError:
                pass

        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > self._max_bytes:
                raise PageTooLarge(f"response exceeds {self._max_bytes} bytes")
            chunks.append(chunk)
        raw = b"".join(chunks)
        try:
            html = raw.decode(response.encoding or "utf-8", errors="replace")
        except LookupError:
            html = raw.decode("utf-8", errors="replace")

        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        try:
            extracted = trafilatura.extract(html)
        except Exception:
            extracted = None
        if not extracted or not extracted.strip():
            for element in soup.select("script, style, nav, noscript"):
                element.decompose()
            extracted = soup.get_text("\n", strip=True)
        text = extracted.strip()

        return FetchedPage(
            requested_url=requested_url,
            final_url=final_url,
            status_code=response.status_code,
            title=title,
            text=text,
            fetched_at=datetime.now(BEIJING),
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
