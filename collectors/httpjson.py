"""Shared read-only JSON-over-HTTP client.

Two collectors now talk to keyless public APIs -- ``collectors/dexscreener.py`` and
``collectors/safety.py`` -- and both need the same three things: a minimum gap
between requests so a shared free endpoint is not hammered, a retry policy that
distinguishes "come back later" from "you asked wrong", and a decoded JSON body.
Writing that twice would mean two retry policies to keep in step, and the one that
drifted would be the one nobody was looking at.

Stdlib only (``urllib.request``), because ``api/screener.py`` runs as a Vercel
function on stdlib alone.

**GET only.** There is no method here that sends a body, and there will not be:
hard rule 4 says this repo holds no exchange keys and places no orders, and a
client that cannot POST cannot be quietly repurposed into one that does.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("httpjson")

USER_AGENT = "crypto-screener/0.1 (phase-0 collector; read-only)"

# Statuses worth another attempt. Everything else -- 400, 401, 404 -- is a bug in
# the call, and retrying it only burns a budget shared with everyone else using the
# endpoint.
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class HttpJsonError(RuntimeError):
    """A request failed, after any retries it was entitled to."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class Throttle:
    """Smallest thing that keeps a caller under a per-minute cap: a minimum gap.

    Spacing requests evenly rather than bursting to the cap and then stalling is
    both kinder to the endpoint and steadier for a long-running watcher. Buckets
    are named by the caller so two endpoints with different limits on the same host
    do not share a budget.

    ``sleep`` and ``clock`` are injectable so a retry test does not actually wait.
    """

    def __init__(
        self,
        limits: dict[str, int] | None = None,
        sleep: Any = time.sleep,
        clock: Any = time.monotonic,
    ) -> None:
        self.limits = dict(limits or {})
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}

    def wait(self, bucket: str) -> None:
        per_minute = self.limits.get(bucket)
        if not per_minute:
            return
        min_gap = 60.0 / per_minute
        last = self._last.get(bucket)
        now = self._clock()
        if last is not None:
            remaining = min_gap - (now - last)
            if remaining > 0:
                self._sleep(remaining)
                now = self._clock()
        self._last[bucket] = now


@dataclass
class JsonGetClient:
    """A read-only JSON GET client with per-bucket throttling and bounded retries."""

    base_url: str = ""
    timeout: float = 20.0
    max_retries: int = 3
    opener: Any = None
    throttle: Throttle = field(default_factory=Throttle)
    sleep: Any = time.sleep
    headers: dict[str, str] = field(default_factory=dict)

    def get(
        self, path: str, bucket: str = "", params: dict[str, str] | None = None
    ) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            **self.headers,
        }

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self.throttle.wait(bucket)
            request = urllib.request.Request(url, headers=request_headers)
            try:
                opener = self.opener or urllib.request.urlopen
                with opener(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code not in RETRYABLE_STATUSES:
                    raise HttpJsonError(f"HTTP {exc.code} for {url}", status=exc.code) from exc
                last_error = exc
            except (
                urllib.error.URLError,
                TimeoutError,
                json.JSONDecodeError,
                OSError,
            ) as exc:
                last_error = exc
            if attempt < self.max_retries:
                backoff = min(2**attempt, 30)
                log.warning(
                    "%s attempt %d/%d failed: %s", path, attempt, self.max_retries, last_error
                )
                self.sleep(backoff)
        raise HttpJsonError(f"failed after {self.max_retries} attempts: {url}: {last_error}")
