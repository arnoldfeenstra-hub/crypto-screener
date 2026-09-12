"""No test opens a socket.

The README claims the suite runs with no network, and that claim was true by
convention until a live-safety lookup slipped into a test and turned a six-second
run into a five-minute one -- retrying, backing off, and passing anyway because
"unknown" is a legitimate answer. A test that quietly reaches the internet is worse
than a slow one: it passes or fails on someone else's uptime, and the failure looks
like a bug in this repo.

So the ban is enforced rather than assumed. Anything that tries to open an HTTP
connection during a test fails immediately, and the message says which URL and what
to do about it: pass a stub client, or a recorded fixture from ``fixtures/``.

``requests`` is patched too where it is installed, because ``collectors/bitquery.py``
uses it.
"""

from __future__ import annotations

import urllib.request

import pytest


class NetworkAccessDuringTest(AssertionError):
    """A test tried to make a real request."""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def blocked(request, *args, **kwargs):
        url = getattr(request, "full_url", request)
        raise NetworkAccessDuringTest(
            f"a test tried to reach {url}. The suite runs offline: pass a stub "
            "opener or client, or a recorded response from fixtures/."
        )

    monkeypatch.setattr(urllib.request, "urlopen", blocked)

    try:
        import requests
    except ImportError:  # pragma: no cover - requests is a declared dependency
        return

    def blocked_session(self, method, url, *args, **kwargs):
        raise NetworkAccessDuringTest(
            f"a test tried to {method} {url}. The suite runs offline."
        )

    monkeypatch.setattr(requests.Session, "request", blocked_session, raising=False)
