"""THE CARPET, live -- ``GET /api/carpet``.

    /api/carpet?group=hl-crypto&read_on=1h        the carpet: every name's readings
    /api/carpet?chart=BTC&interval=5m&bars=800    one name's chart and studies

Reads Hyperliquid's public info endpoint at request time (``rotation/hyperliquid``)
and returns what ``rotation/carpet.py`` and ``rotation/chart.py`` make of it. It
keeps no history and stores nothing. Every reading it returns is descriptive;
none has been tested for predictive value, and there is no code path from here to
an order (hard rule 4).

Stdlib only, like ``api/screener.py``: nothing is installed at deploy time.
"""

from __future__ import annotations

import json
import sys
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rotation.carpet import DEFAULT_GROUP, DEFAULT_READ_ON, build_carpet  # noqa: E402
from rotation.chart import DEFAULT_BARS, build_chart  # noqa: E402
from rotation.hyperliquid import HyperliquidError  # noqa: E402

# One upstream poll per window however many people have the page open. Hyperliquid's
# public endpoint is rate-limited per IP, and a carpet is a dozen requests.
CACHE_SECONDS = 30


def respond(query: str, *, info: Any = None) -> tuple[int, dict[str, Any]]:
    params = urllib.parse.parse_qs(query or "")

    def one(key: str, default: str) -> str:
        return params.get(key, [default])[0].strip()

    try:
        chart = one("chart", "")
        if chart:
            try:
                bars = int(one("bars", str(DEFAULT_BARS)))
            except ValueError:
                bars = DEFAULT_BARS
            return 200, build_chart(chart.upper(), one("interval", "5m"), bars, info=info)
        return 200, build_carpet(
            one("group", DEFAULT_GROUP), one("read_on", DEFAULT_READ_ON), info=info
        )
    except ValueError as exc:
        return 400, {"error": str(exc)}
    except HyperliquidError as exc:
        return 503, {"error": f"Hyperliquid is not answering: {exc}"}
    except Exception as exc:  # pragma: no cover - last-resort guard
        return 500, {
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc(limit=3),
        }


class handler(BaseHTTPRequestHandler):
    """Vercel Python function entry point. GET only; nothing here writes."""

    def do_GET(self) -> None:
        status, payload = respond(urllib.parse.urlparse(self.path).query)
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if status == 200:
            self.send_header(
                "Cache-Control", f"public, s-maxage={CACHE_SECONDS}, stale-while-revalidate=120"
            )
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        """Quiet the default stderr access log; the platform already records it."""
