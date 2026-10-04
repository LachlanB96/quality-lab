"""Fixtures shared by every SpireScape test.

`--target local` (the default) builds the server from SPIRESCAPE_REPO once, then starts one server per
xdist worker with a 20 ms tick and seed 7. `--target live` points at the public demo instead and skips
anything marked local_only.
"""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, settings

from qualitylab.spirescape import server as srv
from qualitylab.spirescape.client import GameClient
from qualitylab.spirescape.play import Budget

pytest_plugins = ["qualitylab.results"]

settings.register_profile("default", deadline=None, max_examples=60,
                          suppress_health_check=[HealthCheck.function_scoped_fixture])
settings.register_profile("live", deadline=None, max_examples=15,
                          suppress_health_check=[HealthCheck.function_scoped_fixture])

LOCAL_TICK_MS = 20
LOCAL_SEED = 7


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--target", choices=("local", "live"), default="local",
                     help="local: a server built from SPIRESCAPE_REPO; live: the public demo")


def pytest_configure(config: pytest.Config) -> None:
    live = config.getoption("--target") == "live"
    settings.load_profile("live" if live else "default")
    # what the run record stores about the target (the controller never starts a server itself)
    config._ql_target = SimpleNamespace(name="live" if live else "local",
                                        tick_ms=600 if live else LOCAL_TICK_MS, seeded=not live)


def pytest_sessionstart(session: pytest.Session) -> None:
    cfg = session.config
    if cfg.getoption("--target") != "local" or hasattr(cfg, "workerinput"):
        return
    if any("tests/unit" in str(a).replace("\\", "/") for a in cfg.args) and len(cfg.args) == 1:
        return  # unit tests only: no server needed
    srv.build(srv.find_repo())  # once, before xdist workers start their own servers


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--target") != "live":
        return
    skip = pytest.mark.skip(reason="local_only: needs the seeded, fast-tick local server")
    for item in items:
        if "local_only" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def target(request: pytest.FixtureRequest):
    if request.config.getoption("--target") == "live":
        t = srv.live_target()
        assert srv.health(t.health_url).startswith("ok"), "live demo is not answering /healthz"
        yield t
        return
    server = srv.LocalServer(srv.find_repo(), tick_ms=LOCAL_TICK_MS, seed=LOCAL_SEED).start()
    request.config._ql_local_server = server
    yield server.target
    server.stop()


@pytest.fixture(scope="session")
def budget(target) -> Budget:
    return Budget(target.tick_ms)


@pytest.fixture
def local_server(request: pytest.FixtureRequest, target) -> srv.LocalServer:
    """The running local server process, for tests that inspect its log or liveness."""
    return request.config._ql_local_server


@pytest.fixture
def connect(request: pytest.FixtureRequest, target):
    """Factory: connect(name) -> a logged-in GameClient.

    After the test every client is closed, and two checks run on what happened during it: every frame
    the server sent decoded strictly, and (locally) the server logged no exception. So every test that
    plays the game is also a protocol-contract and server-health check.
    """
    opened: list[GameClient] = []
    server = getattr(request.config, "_ql_local_server", None)
    errors_before = len(server.errors) if server else 0

    def _connect(name: str | None = None, *, login: bool = True) -> GameClient:
        c = GameClient(target.ws_url)
        opened.append(c)
        if login:
            c.login(name or unique_name())
        return c

    yield _connect
    for c in opened:
        try:
            c.close()
        except Exception:  # noqa: BLE001 - a client the test already broke
            pass
    bad_frames = [e for c in opened for e in c.decode_errors]
    assert not bad_frames, f"server sent frames the strict decoder rejected: {bad_frames[:3]}"
    if server is not None:
        new_errors = server.errors[errors_before:]
        assert not new_errors, "server logged exceptions during this test:\n" + "\n".join(new_errors[:5])


@pytest.fixture
def player(connect) -> GameClient:
    """One fresh, logged-in player at Landfall's spawn."""
    return connect()


def unique_name(prefix: str = "qa") -> str:
    """Player names are letters, digits, space, _ and -, at most 12 characters (server-side rule)."""
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


@pytest.fixture(autouse=True)
def _server_log_on_failure(request: pytest.FixtureRequest):
    """When a local test fails, attach the last server log lines to the report."""
    yield
    rep = getattr(request.node, "rep_call", None)
    server = getattr(request.config, "_ql_local_server", None)
    if rep is not None and rep.failed and server is not None:
        request.node.add_report_section("call", "server log", "\n".join(list(server.log)[-40:]))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, "rep_" + rep.when, rep)


def is_ci() -> bool:
    return bool(os.environ.get("CI"))
