"""PostHog Cloud client for API usage analytics.

Ported from the ocean-acidification dashboard's `posthog_helpers.py`, same
shape and the same deliberate call: PostHog **Cloud**, not self-hosted. The
official self-hosted stack bundles its own ClickHouse, Kafka, Redis, Zookeeper
and MinIO, and this box already runs ~100 GB of ClickHouse for the science
data — duplicating that to count clicks is not a trade worth making.

Configured entirely by environment:

  POSTHOG_API_KEY
      Project API key. **Capture is a silent no-op when unset**, so dev and any
      environment that does not want analytics needs no other change, and a
      missing key can never slow down or break a request.

  POSTHOG_HOST
      Region-specific ingestion host, e.g. https://us.i.posthog.com or
      https://eu.i.posthog.com. Defaults to the US host.

**What is deliberately not instrumented: `/image`.** Playback prefetches
`AHEAD = 8` frames per tick at up to 10 fps (front/app/composables/usePlayback.ts),
so instrumenting the image route would emit hundreds of events a minute from one
person watching one animation — none of which is a decision anybody made. The
same reasoning excluded tile-serving next door; it is just louder here. `/health`,
`/domain`, `/coverage` and `/variables` are out for the same reason: they are
page-load plumbing, not behaviour.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import time
from typing import Optional

from posthog import Posthog

logger = logging.getLogger("enso.analytics")

_api_key = os.getenv("POSTHOG_API_KEY", "")
_host = os.getenv("POSTHOG_HOST", "https://us.i.posthog.com")

# One long-lived client for the process: capture() enqueues onto an internal
# queue that a background thread batches and flushes over HTTP, so a single
# shared instance — not one per request — is the correct usage. `api` runs
# API_WORKERS separate processes, each of which gets its own.
_client: Optional[Posthog] = Posthog(_api_key, host=_host) if _api_key else None


def _is_routable_public_ip(ip: str) -> bool:
    """False for private/loopback/link-local addresses and anything unparsable.

    In production the API sits behind a reverse proxy (the public base URL is
    https://mhw.cioospacificlabs.ca/api), which forwards X-Forwarded-For /
    X-Real-IP. Without those headers `http_request.client.host` is the Docker
    bridge gateway — a private address PostHog cannot geolocate anyway, so
    geoip stays off for those rather than resolving every visitor to nowhere.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved)


def client_ip(http_request) -> str:
    """Best-effort real client IP, preferring the proxy's forwarded header over
    the raw TCP peer.

    This app has no accounts, so an IP stands in for the distinct_id whenever
    the browser has not supplied one — fine for aggregate "which variable and
    which region get looked at" questions, though it under-counts users behind
    shared NAT and over-counts rotating addresses.
    """
    forwarded = http_request.headers.get("x-forwarded-for")
    if forwarded:
        # First entry is the original client; proxies append their own.
        candidate = forwarded.split(",")[0].strip()
        if candidate:
            return candidate
    real_ip = http_request.headers.get("x-real-ip")
    if real_ip and real_ip.strip():
        return real_ip.strip()
    return http_request.client.host if http_request.client else "unknown"


def capture_event(http_request, event: str, properties: Optional[dict] = None) -> None:
    """Best-effort usage event capture. **Never raises.**

    Analytics failing must never be visible in the dashboard, so every path out
    of here is a warning in the log at worst.
    """
    if _client is None:
        return
    try:
        ip = client_ip(http_request)
        # Prefer the frontend's posthog-js distinct_id (stamped onto
        # request.state by SERVER.py's middleware) so one visitor's UI events
        # and the API calls they cause land under a single identity. Non-browser
        # callers — curl, the healthcheck, anyone using the API directly — send
        # no such header and fall back to IP. geoip stays IP-based either way.
        distinct_id = getattr(http_request.state, "distinct_id", None) or ip
        props = dict(properties or {})
        start_time = getattr(http_request.state, "start_time", None)
        if start_time is not None:
            props["duration_ms"] = round((time.perf_counter() - start_time) * 1000, 1)
        _client.capture(
            distinct_id=distinct_id,
            event=event,
            properties=props,
            disable_geoip=not _is_routable_public_ip(ip),
        )
    except Exception:
        logger.warning("PostHog capture failed for event %s", event, exc_info=True)
