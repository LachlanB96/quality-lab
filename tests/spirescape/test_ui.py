"""The WebGL client in a real browser (Playwright). Always the live site: the local server has no web build.

Runs only on `--target live`. Locally the browser is the installed Edge, so no download is needed.
"""
import os

import pytest

from qualitylab.spirescape.server import LIVE_HOST

pytestmark = [pytest.mark.ui, pytest.mark.area("client")]

URL = f"https://{LIVE_HOST}/"


@pytest.fixture(scope="module", autouse=True)
def _live_only(request):
    if request.config.getoption("--target") != "live":
        pytest.skip("browser tests run against the live site only")


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    channel = os.environ.get("QL_BROWSER_CHANNEL", "msedge" if os.name == "nt" else "")
    return {**browser_type_launch_args, **({"channel": channel} if channel else {})}


def test_page_loads_the_unity_player(page):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    response = page.goto(URL, wait_until="domcontentloaded")
    assert response is not None and response.ok
    page.wait_for_selector("canvas", timeout=30_000)
    assert errors == []


@pytest.mark.xfail(reason="BUG-007: the browser tab says 'Unity Web Player | client'", strict=True)
def test_the_tab_is_titled_spirescape(page):
    """What a visitor sees in the tab and in bookmarks. Unity's WebGL template default is left in."""
    page.goto(URL, wait_until="domcontentloaded")
    assert "SpireScape" in page.title(), page.title()


def test_wasm_and_data_are_served(page):
    """The two big downloads, fetched for real (Caddy compresses them on the fly)."""
    seen = {}
    page.on("response", lambda r: seen.__setitem__(r.url.rsplit("/", 1)[-1], r.status))
    page.goto(URL, wait_until="load")
    page.wait_for_timeout(8000)
    wasm = [n for n in seen if n.endswith(".wasm")]
    data = [n for n in seen if n.endswith(".data")]
    assert wasm and data, f"no wasm/data fetched: {sorted(seen)[:10]}"
    assert all(seen[n] in (200, 304) for n in wasm + data)


def test_build_files_must_revalidate(page):
    """Unity keeps file names across builds, so the Caddyfile sends no-cache on /Build/*."""
    headers = {}
    page.on("response", lambda r: headers.update({r.url: r.headers}) if "/Build/" in r.url else None)
    page.goto(URL, wait_until="load")
    page.wait_for_timeout(5000)
    assert headers, "no /Build/ requests seen"
    assert all("no-cache" in h.get("cache-control", "") for h in headers.values())
