from __future__ import annotations

import re
import ssl
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen


class NetworkState(StrEnum):
    ONLINE = "ONLINE"
    CAPTIVE_PORTAL = "CAPTIVE_PORTAL"
    TLS_INTERCEPTED = "TLS_INTERCEPTED"
    OFFLINE = "OFFLINE"


class NetworkAccessError(RuntimeError):
    """Raised before credentials are sent when external HTTPS is not trustworthy."""


@dataclass(frozen=True)
class ConnectivityResult:
    state: NetworkState
    ok: bool
    probe_url: str
    final_url: str | None
    http_status: int | None
    tls_url: str | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["state"] = self.state.value
        return payload


def _safe_host(url: str | None) -> str | None:
    if not url:
        return None
    return urlsplit(url).hostname


def probe_external_access(
    probe_url: str = "http://connectivitycheck.gstatic.com/generate_204",
    *,
    tls_url: str = "https://api.twelvedata.com/",
    timeout_seconds: float = 8.0,
) -> ConnectivityResult:
    """Check the network path without sending provider credentials.

    The HTTP 204 probe detects captive portals. Only after it succeeds do we
    validate a normal public TLS certificate. Certificate verification is never
    disabled and provider API keys are not used by this function.
    """

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    probe_parts = urlsplit(probe_url)
    if probe_parts.scheme not in {"http", "https"} or not probe_parts.hostname:
        raise ValueError("connectivity probe must be an absolute HTTP(S) URL")
    tls_parts = urlsplit(tls_url)
    if tls_parts.scheme != "https" or not tls_parts.hostname:
        raise ValueError("TLS probe must be an absolute HTTPS URL")

    try:
        request = Request(
            probe_url,
            headers={"User-Agent": "TradeLab-Agent connectivity-preflight"},
            method="GET",
        )
        with urlopen(request, timeout=timeout_seconds) as response:
            status = int(getattr(response, "status", response.getcode()))
            final_url = response.geturl()
            body = response.read(2048).decode("utf-8", errors="replace")
    except HTTPError as exc:
        status = int(exc.code)
        final_url = exc.geturl()
        body = exc.read(2048).decode("utf-8", errors="replace")
    except (URLError, TimeoutError, OSError) as exc:
        return ConnectivityResult(
            state=NetworkState.OFFLINE,
            ok=False,
            probe_url=probe_url,
            final_url=None,
            http_status=None,
            tls_url=tls_url,
            detail=f"connectivity probe failed: {type(exc).__name__}: {exc}",
        )

    redirect_match = re.search(
        r"location\.href\s*=\s*['\"]([^'\"]+)",
        body,
        flags=re.IGNORECASE,
    )
    if redirect_match:
        final_url = urljoin(final_url or probe_url, redirect_match.group(1))

    original_host = _safe_host(probe_url)
    final_host = _safe_host(final_url)
    portal_markers = (
        "authentication is required",
        "network access authentication",
        "网络接入认证",
    )
    body_is_portal = any(marker in body.lower() for marker in portal_markers)
    if status != 204 or final_host != original_host or body_is_portal:
        return ConnectivityResult(
            state=NetworkState.CAPTIVE_PORTAL,
            ok=False,
            probe_url=probe_url,
            final_url=final_url,
            http_status=status,
            tls_url=tls_url,
            detail=(
                "external access is behind a captive portal or authentication gateway; "
                f"expected HTTP 204 from {original_host}, received {status} from {final_host}"
            ),
        )

    try:
        tls_request = Request(
            tls_url,
            headers={"User-Agent": "TradeLab-Agent connectivity-preflight"},
            method="GET",
        )
        with urlopen(tls_request, timeout=timeout_seconds) as response:
            response.read(1)
    except ssl.SSLCertVerificationError as exc:
        return ConnectivityResult(
            state=NetworkState.TLS_INTERCEPTED,
            ok=False,
            probe_url=probe_url,
            final_url=final_url,
            http_status=status,
            tls_url=tls_url,
            detail=f"TLS certificate verification failed: {exc}",
        )
    except URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, ssl.SSLCertVerificationError):
            return ConnectivityResult(
                state=NetworkState.TLS_INTERCEPTED,
                ok=False,
                probe_url=probe_url,
                final_url=final_url,
                http_status=status,
                tls_url=tls_url,
                detail=f"TLS certificate verification failed: {reason}",
            )
        return ConnectivityResult(
            state=NetworkState.OFFLINE,
            ok=False,
            probe_url=probe_url,
            final_url=final_url,
            http_status=status,
            tls_url=tls_url,
            detail=f"TLS probe failed: {type(exc).__name__}: {exc}",
        )
    except (TimeoutError, OSError) as exc:
        return ConnectivityResult(
            state=NetworkState.OFFLINE,
            ok=False,
            probe_url=probe_url,
            final_url=final_url,
            http_status=status,
            tls_url=tls_url,
            detail=f"TLS probe failed: {type(exc).__name__}: {exc}",
        )

    return ConnectivityResult(
        state=NetworkState.ONLINE,
        ok=True,
        probe_url=probe_url,
        final_url=final_url,
        http_status=status,
        tls_url=tls_url,
        detail="external HTTPS access is available with normal certificate verification",
    )


def require_external_access(result: ConnectivityResult) -> None:
    if result.ok:
        return
    remediation = (
        "Authenticate the server network or configure a trusted outbound proxy, then retry. "
        "Do not disable TLS verification or send API keys through the captive portal."
    )
    raise NetworkAccessError(f"{result.state.value}: {result.detail}. {remediation}")


__all__ = [
    "ConnectivityResult",
    "NetworkAccessError",
    "NetworkState",
    "probe_external_access",
    "require_external_access",
]
