"""Shared fixtures.

``app.receiver`` keeps its connection state in module globals, so every test has
to leave those globals clean or the next one inherits a live thread. The
``receiver`` fixture below handles setup and teardown.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.fake_caster import FakeCaster


@pytest.fixture
def caster():
    c = FakeCaster()
    yield c
    c.shutdown()


@pytest.fixture
def receiver(tmp_path, monkeypatch):
    """The receiver module, pointed at a throwaway config and wound down after."""
    from app import receiver as mod

    monkeypatch.setattr(mod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(mod, "LOG_DIR", tmp_path / "logs")

    # Shorten every wait so a reconnect cycle fits in a test instead of a minute.
    monkeypatch.setattr(mod, "NTRIP_OUT_RECONNECT_INITIAL", 0.2)
    monkeypatch.setattr(mod, "NTRIP_OUT_RECONNECT_MAX", 0.4)
    monkeypatch.setattr(mod, "NTRIP_IN_RECONNECT_INTERVAL", 0.5)
    monkeypatch.setattr(mod, "NTRIP_IN_GGA_INTERVAL", 0.05)

    mod._stop.clear()
    yield mod

    # Teardown: never leak a worker thread into the next test.
    mod.ntrip_in_disconnect()
    mod.ntrip_out_disconnect()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not any(t.name in ("ntrip-in", "ntrip-out") for t in threading.enumerate()):
            break
        time.sleep(0.02)
    mod._stop.clear()


@pytest.fixture
def fixed_position(receiver):
    """A plausible RTK fix, so _build_gga() produces a sentence worth sending."""
    with receiver._lock:
        receiver.state.lat = 33.0198
        receiver.state.lon = -96.6989
        receiver.state.height_msl_m = 200.0
        receiver.state.height_m = 173.0
        receiver.state.num_sv = 12
        receiver.state.hdop = 0.9
        receiver.state.fix_type = 3
        receiver.state.carrier_solution = 2
        receiver.state.tmode = 1  # survey-in: NTRIP-in is wanted
    return receiver
