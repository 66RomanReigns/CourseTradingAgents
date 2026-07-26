from __future__ import annotations

import gzip
import hashlib
import json
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


SENSITIVE_QUERY_KEYS = {"apikey", "api_key", "token", "access_token", "key"}


class DataApiError(RuntimeError):
    """Raised when a remote data provider returns an invalid or failed response."""


class JsonHttpClient(Protocol):
    def get_json(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        *,
        cache_ttl_seconds: int | None = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]: ...


def _redact_url(url: str) -> str:
    parts = urlsplit(url)
    redacted: list[tuple[str, str]] = []
    if parts.query:
        from urllib.parse import parse_qsl

        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            redacted.append((key, "***" if key.lower() in SENSITIVE_QUERY_KEYS else value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(redacted), parts.fragment))


def _decode_content(
    payload: bytes,
    headers: Mapping[str, str],
    source: str,
    max_response_bytes: int,
) -> bytes:
    normalized_headers = {str(key).lower(): str(value) for key, value in headers.items()}
    encoding = normalized_headers.get("content-encoding", "").lower().strip()
    try:
        if encoding == "gzip":
            decoded = gzip.decompress(payload)
        elif encoding == "deflate":
            try:
                decoded = zlib.decompress(payload)
            except zlib.error:
                decoded = zlib.decompress(payload, -zlib.MAX_WBITS)
        else:
            decoded = payload
    except (OSError, zlib.error) as exc:
        raise DataApiError(
            f"invalid {encoding or 'identity'} response encoding from {_redact_url(source)}: {exc}"
        ) from exc
    if len(decoded) > max_response_bytes:
        raise DataApiError(
            f"decoded response from {_redact_url(source)} exceeds {max_response_bytes} bytes"
        )
    return decoded


def _json_object(payload: bytes, source: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataApiError(f"invalid JSON response from {_redact_url(source)}: {exc}") from exc
    if not isinstance(value, dict):
        raise DataApiError(f"expected JSON object from {_redact_url(source)}")
    return value


@dataclass
class CachedHttpJsonClient:
    cache_dir: Path | str = Path("data/cache/http")
    timeout_seconds: float = 20.0
    max_retries: int = 3
    backoff_seconds: float = 0.6
    max_response_bytes: int = 20 * 1024 * 1024
    default_user_agent: str = "TradeLab-Agent/0.4 (+course-research)"

    def __post_init__(self) -> None:
        self.cache_dir = Path(self.cache_dir)
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")

    @staticmethod
    def _build_url(url: str, params: Mapping[str, Any] | None) -> str:
        if not params:
            return url
        filtered = {key: value for key, value in params.items() if value is not None}
        query = urlencode(filtered, doseq=True)
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}{query}"

    def _cache_path(self, request_url: str, headers: Mapping[str, str]) -> Path:
        authorization = headers.get("Authorization", "")
        identity = json.dumps(
            {
                "url": request_url,
                "accept": headers.get("Accept", "application/json"),
                "user_agent": headers.get("User-Agent", self.default_user_agent),
                "authorization_sha256": (
                    hashlib.sha256(authorization.encode("utf-8")).hexdigest()
                    if authorization
                    else None
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(identity).hexdigest()
        return self.cache_dir / f"{digest}.json"

    @staticmethod
    def _cache_is_fresh(path: Path, ttl_seconds: int | None) -> bool:
        if ttl_seconds is None or ttl_seconds <= 0 or not path.is_file():
            return False
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return datetime.now(timezone.utc) - modified <= timedelta(seconds=ttl_seconds)

    @staticmethod
    def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
        raw = headers.get("Retry-After")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(raw)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    def get_json(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        *,
        cache_ttl_seconds: int | None = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        if urlsplit(url).scheme.lower() != "https":
            raise ValueError("only HTTPS data endpoints are allowed")
        request_url = self._build_url(url, params)
        request_headers = {
            "Accept": "application/json",
            "User-Agent": self.default_user_agent,
            **dict(headers or {}),
        }
        cache_path = self._cache_path(request_url, request_headers)
        if not force_refresh and self._cache_is_fresh(cache_path, cache_ttl_seconds):
            return _json_object(cache_path.read_bytes(), str(cache_path))

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                request = Request(request_url, headers=request_headers, method="GET")
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = response.read(self.max_response_bytes + 1)
                    response_headers = {
                        str(key): str(value) for key, value in response.headers.items()
                    }
                if len(payload) > self.max_response_bytes:
                    raise DataApiError(
                        f"response from {_redact_url(request_url)} exceeds "
                        f"{self.max_response_bytes} bytes"
                    )
                payload = _decode_content(
                    payload,
                    response_headers,
                    request_url,
                    self.max_response_bytes,
                )
                result = _json_object(payload, request_url)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = cache_path.with_suffix(".tmp")
                temporary.write_bytes(payload)
                temporary.replace(cache_path)
                return result
            except HTTPError as exc:
                body = exc.read() if getattr(exc, "fp", None) else b""
                message = body.decode("utf-8", errors="replace")[:500]
                retryable = exc.code == 429 or 500 <= exc.code < 600
                last_error = DataApiError(
                    f"HTTP {exc.code} from {_redact_url(request_url)}: {message or exc.reason}"
                )
                if not retryable or attempt >= self.max_retries:
                    raise last_error from exc
                delay = self._retry_after_seconds(exc.headers) or self.backoff_seconds * (2**attempt)
                time.sleep(delay)
            except (URLError, TimeoutError, OSError) as exc:
                last_error = DataApiError(f"request failed for {_redact_url(request_url)}: {exc}")
                if attempt >= self.max_retries:
                    raise last_error from exc
                time.sleep(self.backoff_seconds * (2**attempt))

        raise DataApiError(str(last_error or "unknown HTTP error"))
