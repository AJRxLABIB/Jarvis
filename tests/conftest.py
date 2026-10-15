from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.config.settings import ENV_NAMES
from app.utils.logging_setup import teardown_logging


@pytest.fixture(autouse=True)
def assume_online(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never probe the real internet; OFFLINE_MODE=auto sees a working connection."""
    from app.utils import network

    status = network.NetworkStatus(check=lambda: True)
    monkeypatch.setattr(network, "MONITOR", status)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate tests from the developer's real environment variables."""
    for env_name in ENV_NAMES.values():
        monkeypatch.delenv(env_name, raising=False)
    yield
    teardown_logging()  # release log files so tmp folders can be removed on Windows


@pytest.fixture(autouse=True)
def forget_pinned_browser_profile() -> Iterator[None]:
    """Each test starts without a remembered browser profile (it's per-process state)."""
    from app.tools import browser_profiles

    browser_profiles._pins.clear()
    browser_profiles._loaded_from.clear()
    yield
    browser_profiles._pins.clear()
    browser_profiles._loaded_from.clear()


@pytest.fixture(autouse=True)
def quick_browser_self_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools import applications

    monkeypatch.setattr(applications, "BROWSER_START_SECONDS", 0.2)


@pytest.fixture(autouse=True)
def fresh_modes_emergency_and_events() -> Iterator[None]:
    """Modes, emergency switches and the event bus are process-wide: every test starts clean."""
    from app.core import emergency, events, modes

    modes.STATE = modes.ModeState()
    emergency.STATE = emergency.Emergency()
    events.BUS.clear()
    yield
    modes.STATE = modes.ModeState()
    emergency.STATE = emergency.Emergency()
    events.BUS.clear()


@pytest.fixture(autouse=True)
def no_software_dimming() -> Iterator[None]:
    from app.tools import windows

    windows.SOFT["level"] = 100
    yield
    windows.SOFT["level"] = 100


@pytest.fixture(autouse=True)
def fresh_context_engine() -> Iterator[None]:
    from app.core import context_engine

    context_engine.ENGINE.clear()
    context_engine.ENGINE.pending_approval = None
    context_engine.ENGINE.last_request = ""
    yield
    context_engine.ENGINE.clear()
    context_engine.ENGINE.pending_approval = None


@pytest.fixture(autouse=True)
def fresh_proactive_engine() -> Iterator[None]:
    from app.core import proactive

    proactive.ENGINE.clear()
    yield
    proactive.ENGINE.clear()
