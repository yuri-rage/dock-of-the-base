"""Lifecycle tests for the NTRIP input and output paths in app/receiver.py.

Each test here corresponds to a bug that was found by running this code rather
than reading it. The invariant they all defend is the same one:

    at most one session per mount point, at any moment, on either direction

Duplicate sessions are the failure mode this module has produced repeatedly.
On the output side a duplicate means two SOURCE uploads to one mount, which many
casters reject outright. On the input side it means two threads writing
corrections into the same serial port, interleaving partial RTCM frames.

The bugs these cover:

1. ``ntrip_out_connect`` opened a socket, discarded it, and let the worker open a
   second one -- an orphaned upload on every connect.
2. ``ntrip_out_disconnect`` called ``close()`` on a socket another thread was
   blocked in ``recv()`` on. On Linux the blocked call holds a reference to the
   file description, so no FIN was sent and the session stayed up until the 30s
   socket timeout expired.
3. A superseded worker parked in its reconnect backoff would wake, observe that
   a *new* connect had set the active flag back to True, and resurrect itself as
   a second live session. Fixed on the output path first, then the input path.
4. Connection state was mutated from several threads without a lock.
"""

from __future__ import annotations

import ast
import threading
import time
from pathlib import Path

import pytest

from tests.fake_caster import OVERLAP_TOLERANCE_S

REPO_ROOT = Path(__file__).resolve().parent.parent
RECEIVER_SRC = REPO_ROOT / "app" / "receiver.py"


def _write_config(receiver, **sections) -> None:
    import json

    receiver.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    receiver.CONFIG_PATH.write_text(json.dumps(sections))


def _worker_count(*names: str) -> int:
    return sum(1 for t in threading.enumerate() if t.name in names)


def _settle(seconds: float = 1.0) -> None:
    time.sleep(seconds)


# --------------------------------------------------------------------------- #
# NTRIP-out
# --------------------------------------------------------------------------- #


def test_out_connect_opens_exactly_one_session(receiver, caster):
    _write_config(receiver, external_caster=caster.config())

    assert receiver.ntrip_out_connect() is None
    assert caster.wait_for("out", 1), "caster never saw the upload session"

    _settle(0.5)
    assert caster.live("out") == 1
    assert caster.total("out") == 1, (
        "connect opened more than one socket -- the authenticated socket is "
        "being discarded and reopened by the worker"
    )


def test_out_reconnect_does_not_leave_a_stale_session(receiver, caster):
    """Regression: the stale session used to linger for the full 30s timeout.

    ``close()`` alone does not interrupt a thread blocked in ``recv()``; the
    teardown path needs ``shutdown(SHUT_RDWR)`` first.
    """
    _write_config(receiver, external_caster=caster.config())

    receiver.ntrip_out_connect()
    assert caster.wait_for("out", 1)

    receiver.ntrip_out_connect()  # reconnect without an explicit disconnect
    _settle(3.0)

    assert caster.live("out") == 1, (
        f"{caster.live('out')} concurrent upload sessions 3s after reconnect; "
        "the superseded socket is not being shut down promptly"
    )
    assert caster.max_overlap("out") < OVERLAP_TOLERANCE_S


def test_out_disconnect_is_prompt(receiver, caster):
    """Disconnect must drop the session immediately, not at socket timeout."""
    _write_config(receiver, external_caster=caster.config())

    receiver.ntrip_out_connect()
    assert caster.wait_for("out", 1)

    t0 = time.monotonic()
    receiver.ntrip_out_disconnect()
    assert caster.wait_for("out", 0, timeout=5.0), (
        "caster still sees the session 5s after disconnect"
    )
    assert time.monotonic() - t0 < 2.0


def test_out_survives_connect_disconnect_churn(receiver, caster):
    _write_config(receiver, external_caster=caster.config())

    for _ in range(25):
        receiver.ntrip_out_connect()
        time.sleep(0.03)
        receiver.ntrip_out_disconnect()
        time.sleep(0.02)

    _settle(2.0)
    assert caster.max_overlap("out") < OVERLAP_TOLERANCE_S
    assert caster.live("out") == 0
    assert _worker_count("ntrip-out") == 0, "worker threads leaked during churn"
    assert receiver._ntrip_out_active is False
    assert receiver._ntrip_out_status == "disconnected"
    assert receiver._ntrip_out_sock is None


def test_out_reconnect_during_backoff_does_not_resurrect_worker(receiver, caster):
    """A worker parked in backoff must not rejoin when a new session starts.

    It used to re-read the shared active flag, see the *new* session's True, and
    carry on as a second uploader.
    """
    _write_config(receiver, external_caster=caster.config())

    caster.reject_next_handshake()
    receiver.ntrip_out_connect()  # fails, worker enters backoff
    time.sleep(0.05)

    receiver.ntrip_out_connect()  # succeeds while the old worker sleeps
    _settle(2.0)

    assert caster.live("out") <= 1
    assert caster.max_overlap("out") < OVERLAP_TOLERANCE_S
    assert _worker_count("ntrip-out") <= 1, "a superseded worker resurrected itself"


# --------------------------------------------------------------------------- #
# NTRIP-in
# --------------------------------------------------------------------------- #


def test_in_connect_opens_exactly_one_session(fixed_position, caster):
    receiver = fixed_position
    _write_config(receiver, ntrip_in=dict(caster.config(), username="u", password="p"))

    assert receiver.ntrip_in_connect() is None
    assert caster.wait_for("in", 1)

    _settle(0.5)
    assert caster.live("in") == 1
    assert caster.total("in") == 1


def test_in_reconnect_during_backoff_does_not_resurrect_worker(fixed_position, caster):
    """Regression: reconnecting inside the backoff window spawned a zombie.

    Timeline before the fix, with a 1s reconnect interval::

        +0.00s session opens
        +0.42s disconnect closes it
        +0.51s reconnect opens a new session
        +1.40s the OLD worker wakes, sees active=True again, opens a THIRD

    In production the reconnect interval is 30s, so any reconnect within half a
    minute of a disconnect hit this.
    """
    receiver = fixed_position
    _write_config(receiver, ntrip_in=dict(caster.config(), username="u", password="p"))

    receiver.ntrip_in_connect()
    assert caster.wait_for("in", 1)

    receiver.ntrip_in_disconnect()
    time.sleep(0.1)  # worker is now parked in its reconnect sleep
    receiver.ntrip_in_connect()

    # Sleep past the reconnect interval so a zombie would have woken by now.
    _settle(receiver.NTRIP_IN_RECONNECT_INTERVAL * 3)

    assert caster.live("in") == 1, (
        f"{caster.live('in')} concurrent source sessions; a superseded worker "
        "woke from backoff and reconnected itself"
    )
    assert _worker_count("ntrip-in") == 1


def test_in_disconnect_is_prompt(fixed_position, caster):
    receiver = fixed_position
    _write_config(receiver, ntrip_in=dict(caster.config(), username="u", password="p"))

    receiver.ntrip_in_connect()
    assert caster.wait_for("in", 1)

    t0 = time.monotonic()
    receiver.ntrip_in_disconnect()
    assert caster.wait_for("in", 0, timeout=5.0)
    assert time.monotonic() - t0 < 2.0


def test_in_survives_connect_disconnect_churn(fixed_position, caster):
    receiver = fixed_position
    _write_config(receiver, ntrip_in=dict(caster.config(), username="u", password="p"))

    for _ in range(25):
        receiver.ntrip_in_connect()
        time.sleep(0.04)
        receiver.ntrip_in_disconnect()
        time.sleep(0.02)

    _settle(2.0)
    assert caster.max_overlap("in") < OVERLAP_TOLERANCE_S
    assert caster.live("in") == 0
    assert _worker_count("ntrip-in") == 0
    assert receiver._ntrip_in_active is False
    assert receiver._ntrip_in_status == "disconnected"
    assert receiver._ntrip_in_sock is None


def test_in_stops_when_receiver_enters_fixed_mode(fixed_position, caster):
    """Corrections input is pointless in Fixed mode, so the worker should exit."""
    receiver = fixed_position
    _write_config(receiver, ntrip_in=dict(caster.config(), username="u", password="p"))

    receiver.ntrip_in_connect()
    assert caster.wait_for("in", 1)
    assert receiver.ntrip_in_active() is True

    with receiver._lock:
        receiver.state.tmode = 2  # Fixed

    deadline = time.monotonic() + 5.0
    while receiver.ntrip_in_active() and time.monotonic() < deadline:
        time.sleep(0.02)

    assert receiver.ntrip_in_active() is False
    assert caster.wait_for("in", 0, timeout=5.0)


def test_in_connect_refused_in_fixed_mode(fixed_position, caster):
    receiver = fixed_position
    _write_config(receiver, ntrip_in=dict(caster.config(), username="u", password="p"))
    with receiver._lock:
        receiver.state.tmode = 2

    assert receiver.ntrip_in_connect() is not None  # returns an error string
    _settle(0.3)
    assert caster.live("in") == 0


# --------------------------------------------------------------------------- #
# Both directions at once
# --------------------------------------------------------------------------- #


def test_both_directions_under_contention(fixed_position, caster):
    """Churn both paths while hammering the status accessors and forward path.

    The status readers, the per-message RTCM forward, and the state lock all
    contend here. Anything that blocks for more than a few seconds is a
    deadlock, and any sustained duplicate session is a lifecycle bug.
    """
    receiver = fixed_position
    _write_config(
        receiver,
        external_caster=caster.config(),
        ntrip_in=dict(caster.config(), username="u", password="p"),
    )

    stop = threading.Event()
    slow: list[str] = []

    def timed(name, fn):
        t0 = time.monotonic()
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            slow.append(f"{name} raised {type(exc).__name__}: {exc}")
        elapsed = time.monotonic() - t0
        if elapsed > 5.0:
            slow.append(f"{name} blocked for {elapsed:.1f}s")

    def churn(connect, disconnect, label):
        while not stop.is_set():
            timed(f"{label}_connect", connect)
            time.sleep(0.03)
            timed(f"{label}_disconnect", disconnect)
            time.sleep(0.02)

    def forward():
        frame = b"\xd3\x00\x13" + b"\x00" * 20
        while not stop.is_set():
            timed("_forward_ntrip_out", lambda: receiver._forward_ntrip_out(frame))
            time.sleep(0.001)

    def poll():
        readers = (
            "ntrip_out_status_str",
            "ntrip_out_connected",
            "ntrip_in_status_str",
            "ntrip_in_connected",
            "ntrip_in_active",
        )
        while not stop.is_set():
            for name in readers:
                timed(name, getattr(receiver, name))
            timed("_build_gga", receiver._build_gga)
            time.sleep(0.002)

    workers = [
        threading.Thread(
            target=churn,
            args=(receiver.ntrip_out_connect, receiver.ntrip_out_disconnect, "out"),
            daemon=True,
        ),
        threading.Thread(
            target=churn,
            args=(receiver.ntrip_in_connect, receiver.ntrip_in_disconnect, "in"),
            daemon=True,
        ),
        threading.Thread(target=forward, daemon=True),
        threading.Thread(target=poll, daemon=True),
        threading.Thread(target=poll, daemon=True),
    ]
    for w in workers:
        w.start()

    time.sleep(12)
    stop.set()
    for w in workers:
        w.join(timeout=10)

    assert not [w for w in workers if w.is_alive()], "a worker thread hung"
    assert not slow, "; ".join(slow[:5])
    assert caster.max_overlap("out") < OVERLAP_TOLERANCE_S
    assert caster.max_overlap("in") < OVERLAP_TOLERANCE_S

    receiver.ntrip_out_disconnect()
    receiver.ntrip_in_disconnect()
    _settle(2.0)
    assert _worker_count("ntrip-in", "ntrip-out") == 0


# --------------------------------------------------------------------------- #
# Static audit
# --------------------------------------------------------------------------- #


def _find_module_locks(tree: ast.Module) -> set[str]:
    """Find all top-level variables initialized with threading.Lock() or Lock()."""
    locks = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
            func = stmt.value.func
            if (isinstance(func, ast.Attribute) and func.attr == "Lock") or (
                isinstance(func, ast.Name) and func.id == "Lock"
            ):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        locks.add(target.id)
    return locks


def _lock_names_in(with_node: ast.With, lock_names: set[str]) -> set[str]:
    return {
        item.context_expr.id
        for item in with_node.items
        if isinstance(item.context_expr, ast.Name)
        and item.context_expr.id in lock_names
    }


def test_no_deadlock_cycles_in_lock_acquisition():
    """Dynamically discover all locks in receiver.py and verify lock acquisition is acyclic.

    receiver.py acquires locks in a strict hierarchy (_config_lock -> _serial_lock).
    This test constructs the acquisition graph across all functions and verifies no
    cycles exist, preventing deadlocks.
    """
    tree = ast.parse(RECEIVER_SRC.read_text())
    lock_names = _find_module_locks(tree)
    assert len(lock_names) >= 7, f"Expected module locks, found: {lock_names}"

    functions = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    acquired_by: dict[str, set[str]] = {}
    for fn in functions:
        held = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.With):
                held |= _lock_names_in(node, lock_names)
        if held:
            acquired_by[fn.name] = held

    edges: set[tuple[str, str]] = set()
    for fn in functions:
        for node in ast.walk(fn):
            if not isinstance(node, ast.With):
                continue
            outer = _lock_names_in(node, lock_names)
            if not outer:
                continue
            for inner in ast.walk(node):
                if inner is node:
                    continue
                if isinstance(inner, ast.With):
                    nested = _lock_names_in(inner, lock_names)
                    for o in outer:
                        for i in nested:
                            if o != i:
                                edges.add((o, i))
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id in acquired_by
                ):
                    for o in outer:
                        for i in acquired_by[inner.func.id]:
                            if o != i:
                                edges.add((o, i))

    # Cycle detection via DFS
    graph: dict[str, set[str]] = {lock: set() for lock in lock_names}
    for o, i in edges:
        graph[o].add(i)

    def find_cycle(node: str, visited: set[str], stack: list[str]) -> list[str] | None:
        visited.add(node)
        stack.append(node)
        for neighbor in graph.get(node, ()):
            if neighbor not in visited:
                cycle = find_cycle(neighbor, visited, stack)
                if cycle:
                    return cycle
            elif neighbor in stack:
                return stack[stack.index(neighbor) :] + [neighbor]
        stack.pop()
        return None

    visited: set[str] = set()
    for lock in lock_names:
        if lock not in visited:
            cycle = find_cycle(lock, visited, [])
            assert cycle is None, (
                f"Deadlock cycle detected in lock acquisition: {' -> '.join(cycle)}"
            )


@pytest.mark.parametrize(
    "func_name",
    ["ntrip_out_disconnect", "ntrip_in_disconnect"],
)
def test_disconnect_shuts_down_before_closing(func_name):
    """``close()`` alone leaves a blocked ``recv()`` hanging until timeout.

    This is the source-level guard for the behaviour asserted by the
    ``*_disconnect_is_prompt`` tests; it fails loudly if someone drops the
    ``shutdown()`` call during a refactor.
    """
    tree = ast.parse(RECEIVER_SRC.read_text())
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == func_name
    )
    calls = [
        node.func.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "shutdown" in calls, f"{func_name} must shutdown() before close()"
    assert "close" in calls
    assert calls.index("shutdown") < calls.index("close")
