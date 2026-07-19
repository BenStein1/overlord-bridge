#!/usr/bin/env python3
"""Overlord fetch relay: a localhost-only, headless-browser fetch endpoint.

Both the Claude and Codex Overlord brains have a WebFetch-style tool, but it
domain-blocks some sites outright (e.g. reddit.com) and plain-curl fallbacks
get bot-walled by anti-scraping pages that fingerprint a missing JS engine.
This relay renders pages with a real headless Chromium (via Playwright) so
the request looks like an ordinary browser, then hands back the rendered HTML
and/or a readability-extracted plain-text version over a tiny local HTTP API:

    GET /fetch?url=<url>[&format=text|html|json][&include_html=1]

Anonymous only: every request gets a fresh, throwaway browser context (no
cookies/local-storage/user-data-dir persisted between requests, no login
flows). Ben doesn't use a Reddit account on this desktop, so there is
deliberately no credential storage here.

Security posture: binds 127.0.0.1 only (refuses to start otherwise, so a
misconfigured .env can't turn this into an open proxy), rejects non-http(s)
URLs, and bounds every fetch with a hard wall-clock timeout.

Run standalone (matches ``bridge.py``'s direct-execution style, no package
imports from sibling modules needed):

    .venv/bin/python modules/fetch_relay.py

Or via ``overlord-fetch-relay.service`` (systemd --user unit, same pattern as
``overlord-bridge.service``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from playwright.async_api import Browser, Playwright, TimeoutError as PlaywrightTimeoutError, async_playwright
from playwright_stealth import Stealth
import trafilatura

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("overlord.fetch_relay")

BASE_DIR = Path(__file__).resolve().parent.parent


def load_env(path: Path) -> None:
    """Minimal .env loader (no external dependency); mirrors bridge.py."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env(BASE_DIR / ".env")

# Never let this become an open proxy: only ever bind loopback, regardless of
# what a misconfigured .env asks for.
_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost"}
HOST = os.environ.get("FETCH_RELAY_HOST", "127.0.0.1").strip() or "127.0.0.1"
if HOST not in _ALLOWED_HOSTS:
    sys.exit(
        f"ERROR: FETCH_RELAY_HOST={HOST!r} is not loopback; refusing to start "
        f"(allowed: {sorted(_ALLOWED_HOSTS)})"
    )

PORT = int(os.environ.get("FETCH_RELAY_PORT", "8791"))
NAV_TIMEOUT_SECONDS = float(os.environ.get("FETCH_RELAY_TIMEOUT_SECONDS", "20"))
NAV_TIMEOUT_MS = NAV_TIMEOUT_SECONDS * 1000
MAX_CONCURRENCY = max(1, int(os.environ.get("FETCH_RELAY_MAX_CONCURRENCY", "3")))
MAX_TEXT_CHARS = max(1000, int(os.environ.get("FETCH_RELAY_MAX_CHARS", "200000")))

_ALLOWED_FORMATS = ("text", "html", "json")
_STEALTH = Stealth()


async def _render(browser: Browser, url: str) -> tuple[str, str, str, int | None]:
    """Render *url* in a fresh, throwaway context. Returns (html, title, final_url, status)."""
    context = await browser.new_context(viewport={"width": 1366, "height": 900})
    await _STEALTH.apply_stealth_async(context)
    try:
        page = await context.new_page()
        try:
            response = await page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                pass  # best-effort; pages with live sockets/ads never go idle
            html = await page.content()
            title = await page.title()
            status = response.status if response else None
            final_url = page.url
        finally:
            await page.close()
    finally:
        await context.close()
    return html, title, final_url, status


async def handle_fetch(request: web.Request) -> web.StreamResponse:
    url = request.query.get("url", "").strip()
    if not url:
        raise web.HTTPBadRequest(text="missing required query param: url")

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise web.HTTPBadRequest(text="url must be http:// or https://")

    fmt = request.query.get("format", "text").strip().lower()
    if fmt not in _ALLOWED_FORMATS:
        raise web.HTTPBadRequest(text=f"format must be one of: {', '.join(_ALLOWED_FORMATS)}")
    include_html = request.query.get("include_html", "").strip().lower() in ("1", "true", "yes", "on")

    browser: Browser = request.app["browser"]
    semaphore: asyncio.Semaphore = request.app["semaphore"]

    try:
        async with semaphore:
            html, title, final_url, status = await asyncio.wait_for(
                _render(browser, url), timeout=NAV_TIMEOUT_SECONDS * 2
            )
    except (PlaywrightTimeoutError, asyncio.TimeoutError):
        return web.json_response({"error": "timeout fetching url", "url": url}, status=504)
    except Exception as exc:  # noqa: BLE001 - surface any render failure to the caller
        log.warning("Fetch failed for %s: %s", url, exc)
        return web.json_response({"error": str(exc), "url": url}, status=502)

    if fmt == "html":
        return web.Response(text=html[: MAX_TEXT_CHARS * 4], content_type="text/html")

    text = trafilatura.extract(
        html, url=final_url, include_comments=False, include_tables=True, favor_recall=True
    ) or ""
    text = text[:MAX_TEXT_CHARS]

    if fmt == "json":
        payload = {"url": url, "final_url": final_url, "status": status, "title": title, "text": text}
        if include_html:
            payload["html"] = html[: MAX_TEXT_CHARS * 4]
        return web.json_response(payload)

    return web.Response(text=text or "(no readable text extracted)", content_type="text/plain")


async def handle_healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def on_startup(app: web.Application) -> None:
    playwright: Playwright = await async_playwright().start()
    app["playwright"] = playwright
    app["browser"] = await playwright.chromium.launch(headless=True)
    app["semaphore"] = asyncio.Semaphore(MAX_CONCURRENCY)
    log.info("Chromium launched (headless, max_concurrency=%d)", MAX_CONCURRENCY)


async def on_cleanup(app: web.Application) -> None:
    await app["browser"].close()
    await app["playwright"].stop()


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/fetch", handle_fetch)
    app.router.add_get("/healthz", handle_healthz)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    log.info("Overlord fetch relay listening on http://%s:%s (loopback only)", HOST, PORT)
    web.run_app(build_app(), host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
