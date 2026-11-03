"""
Unit and integration tests for MAVLink RTCM3 UDP transceiver (app.mavlink).
"""

from __future__ import annotations

import os
import socket
import time
from types import SimpleNamespace

import pytest
from pymavlink.dialects.v20 import common as mavlink2

from app import mavlink
from tests.fake_rover import FakeRover


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_rtcm_frame(total_len: int, msg_type: int = 1005) -> bytes:
    """Create a synthetic RTCM3-prefixed frame of exact total_len bytes."""
    assert total_len >= 6
    b3 = (msg_type >> 4) & 0xFF
    b4 = (msg_type & 0x0F) << 4
    body = os.urandom(total_len - 5)
    return bytes([0xD3, 0x00, 0x10, b3, b4]) + body


def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture(autouse=True)
def _cleanup_mavlink(monkeypatch):
    monkeypatch.setattr(mavlink, "HEARTBEAT_INTERVAL_S", 0.1)
    monkeypatch.setattr(mavlink, "VERSION_RETRY_INTERVAL_S", 0.1)
    monkeypatch.setattr(mavlink, "MIN_TIMEOUT_S", 0.05)
    yield
    mavlink.stop()


# --- Pure unit tests ---


def test_fragment_rtcm3_unfragmented():
    raw = _make_rtcm_frame(25)
    frags = mavlink.fragment_rtcm3(raw, seq_id=7)
    assert len(frags) == 1
    flags, length, data = frags[0]
    assert (flags & 0x01) == 0
    assert ((flags >> 3) & 0x1F) == 7
    assert length == 25
    assert bytes(data[:length]) == raw
    assert len(data) == 180


@pytest.mark.parametrize(
    ("size", "expected_frags", "last_len"),
    [
        (179, 1, 179),
        (180, 1, 180),
        (181, 2, 1),
        (360, 3, 0),
        (500, 3, 140),
        (540, 4, 0),
        (719, 4, 179),
        (720, 4, 180),
    ],
)
def test_fragment_rtcm3_boundaries(size: int, expected_frags: int, last_len: int):
    raw = _make_rtcm_frame(size)
    frags = mavlink.fragment_rtcm3(raw, seq_id=19)
    assert len(frags) == expected_frags
    assert frags[-1][1] == last_len
    reassembled = b"".join(bytes(d[:length]) for _, length, d in frags)
    assert reassembled == raw
    if size <= 180:
        assert (frags[0][0] & 0x01) == 0
    else:
        for idx, (flags, _, _) in enumerate(frags):
            assert (flags & 0x01) == 1
            assert ((flags >> 1) & 0x03) == idx
            assert ((flags >> 3) & 0x1F) == 19


@pytest.mark.parametrize("bad_size", [0, 721, 1024])
def test_fragment_rtcm3_rejects_empty_and_oversize(bad_size: int):
    raw = b"" if bad_size == 0 else _make_rtcm_frame(bad_size)
    assert mavlink.fragment_rtcm3(raw, seq_id=1) == []


def test_fake_rover_ardupilot_reassembly_out_of_order_and_duplicates():
    rover = FakeRover(sysid=1)
    try:
        raw_500 = _make_rtcm_frame(500)
        frags = mavlink.fragment_rtcm3(raw_500, seq_id=5)
        assert len(frags) == 3

        # Deliver out of order: frag 2, duplicate frag 2, frag 0, frag 1
        for idx in (2, 2, 0, 1):
            flags, length, data = frags[idx]
            rover.handle_rtcm_fragment(
                SimpleNamespace(flags=flags, len=length, data=data)
            )
        assert rover.reassembled_rtcm == [raw_500]

        # Deliver 720-byte frame (4 full 180-byte fragments, no short terminator)
        raw_720 = _make_rtcm_frame(720)
        frags_720 = mavlink.fragment_rtcm3(raw_720, seq_id=6)
        assert len(frags_720) == 4
        for idx in (3, 1, 0, 2):
            flags, length, data = frags_720[idx]
            rover.handle_rtcm_fragment(
                SimpleNamespace(flags=flags, len=length, data=data)
            )
        assert rover.reassembled_rtcm == [raw_500, raw_720]
    finally:
        rover.shutdown()


def test_decode_autopilot_version_and_label():
    flight_sw = (4 << 24) | (6 << 16) | (2 << 8) | 255
    ver_str, git_hash = mavlink.decode_autopilot_version(flight_sw, b"3a4b5c6d")
    assert ver_str == "4.6.2"
    assert git_hash == "3a4b5c6d"
    label = mavlink.format_vehicle_label(
        mavlink2.MAV_AUTOPILOT_ARDUPILOTMEGA,
        mavlink2.MAV_TYPE_QUADROTOR,
        ver_str,
    )
    assert label == "Copter 4.6.2"

    blimp_label = mavlink.format_vehicle_label(
        mavlink2.MAV_AUTOPILOT_ARDUPILOTMEGA,
        mavlink2.MAV_TYPE_AIRSHIP,
        "4.6.0",
    )
    assert blimp_label == "Blimp 4.6.0"

    fallback_label = mavlink.format_vehicle_label(
        mavlink2.MAV_AUTOPILOT_ARDUPILOTMEGA,
        mavlink2.MAV_TYPE_GENERIC,
        "4.6.0",
    )
    assert fallback_label == "ArduPilot 4.6.0"


# --- Live UDP lifecycle tests ---


def test_gated_streaming_and_roundtrip_reassembly():
    port = _free_udp_port()
    err = mavlink.start(
        {
            "enabled": True,
            "port": port,
            "broadcast_host": "127.0.0.1",
            "sysid": 250,
            "timeout_s": 5.0,
        }
    )
    assert err is None

    rover = FakeRover(sysid=1, mav_type=mavlink2.MAV_TYPE_QUADROTOR)
    try:
        # Before any heartbeat: RTCM must NOT be sent
        mavlink.forward_mavlink(_make_rtcm_frame(100))
        snap = mavlink.status_snapshot()
        assert snap["active_rovers"] == 0
        assert snap["rtcm_frames_sent"] == 0

        # Rover sends heartbeat and GPS_RAW_INT
        rover.send_heartbeat(port)
        rover.send_gps_raw_int(port, fix_type=6, satellites_visible=19, h_acc_mm=14)

        assert _wait_until(
            lambda: (
                len(mavlink.status_snapshot()["rovers"]) == 1
                and mavlink.status_snapshot()["rovers"][0]["firmware_version"]
                == "4.6.2"
                and mavlink.status_snapshot()["rovers"][0]["fix_name"] == "RTK Fixed"
            )
        )

        # Wait for at least one base heartbeat to arrive at the rover
        assert _wait_until(lambda: len(rover.received_heartbeats) >= 1)
        assert (
            rover.received_heartbeats[0].get_srcComponent()
            == mavlink2.MAV_COMP_ID_ONBOARD_COMPUTER
        )
        assert rover.received_commands[0].confirmation == 0

        snap = mavlink.status_snapshot()
        r_info = snap["rovers"][0]
        assert r_info["sysid"] == 1
        assert r_info["satellites"] == 19
        assert r_info["h_acc_m"] == pytest.approx(0.014)
        assert r_info["label"] == "Copter 4.6.2"

        # Stream RTCM frames across all fragmentation edge cases (including 180 and 720)
        test_sizes = [25, 180, 360, 500, 540, 719, 720]
        sent_frames = [_make_rtcm_frame(sz) for sz in test_sizes]
        for frame in sent_frames:
            mavlink.forward_mavlink(frame)

        # Oversized frames (> 720 bytes: 721 and 850) are dropped and counted
        mavlink.forward_mavlink(_make_rtcm_frame(721, msg_type=1077))
        mavlink.forward_mavlink(_make_rtcm_frame(850, msg_type=1077))

        assert _wait_until(lambda: len(rover.reassembled_rtcm) == len(sent_frames))
        assert rover.reassembled_rtcm == sent_frames

        snap2 = mavlink.status_snapshot()
        assert snap2["rtcm_frames_sent"] == len(sent_frames)
        assert snap2["rtcm_dropped_oversize"] == 2
        assert snap2["rtcm_dropped_by_type"] == {1077: 2}
    finally:
        rover.shutdown()


def test_same_sysid_different_endpoints_do_not_collapse():
    port = _free_udp_port()
    assert (
        mavlink.start(
            {
                "enabled": True,
                "port": port,
                "broadcast_host": "",
                "sysid": 250,
                "timeout_s": 5.0,
            }
        )
        is None
    )

    rover_a = FakeRover(sysid=1, mav_type=mavlink2.MAV_TYPE_QUADROTOR)
    rover_b = FakeRover(sysid=1, mav_type=mavlink2.MAV_TYPE_GROUND_ROVER)
    try:
        rover_a.send_heartbeat(port)
        rover_b.send_heartbeat(port)
        rover_a.send_gps_raw_int(port, fix_type=6, satellites_visible=20)
        rover_b.send_gps_raw_int(port, fix_type=5, satellites_visible=14)

        assert _wait_until(lambda: mavlink.status_snapshot()["active_rovers"] == 2)
        snap = mavlink.status_snapshot()
        assert len(snap["rovers"]) == 2
        endpoints = {r["endpoint"] for r in snap["rovers"]}
        assert endpoints == {f"127.0.0.1:{rover_a.port}", f"127.0.0.1:{rover_b.port}"}

        frames = [_make_rtcm_frame(120) for _ in range(5)]
        for f in frames:
            mavlink.forward_mavlink(f)

        assert _wait_until(
            lambda: (
                len(rover_a.reassembled_rtcm) == 5
                and len(rover_b.reassembled_rtcm) == 5
            )
        )
        assert rover_a.reassembled_rtcm == frames
        assert rover_b.reassembled_rtcm == frames
    finally:
        rover_a.shutdown()
        rover_b.shutdown()


def test_port_conflict_fails_loudly_and_preserves_running_service():
    initial_port = _free_udp_port()
    assert (
        mavlink.start(
            {
                "enabled": True,
                "port": initial_port,
                "broadcast_host": "",
                "sysid": 250,
                "timeout_s": 5.0,
            }
        )
        is None
    )
    assert mavlink.status_snapshot()["enabled"] is True

    conflict_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    conflict_sock.bind(("0.0.0.0", 0))
    busy_port = conflict_sock.getsockname()[1]
    try:
        err = mavlink.start(
            {
                "enabled": True,
                "port": busy_port,
                "broadcast_host": "",
                "sysid": 250,
                "timeout_s": 5.0,
            }
        )
        assert err is not None
        assert f"Failed to bind UDP port {busy_port}" in err
        # The running service on initial_port must remain active and unaffected
        snap = mavlink.status_snapshot()
        assert snap["enabled"] is True
        assert snap["port"] == initial_port
    finally:
        conflict_sock.close()


def test_filters_non_vehicles_and_self_sysid():
    port = _free_udp_port()
    assert (
        mavlink.start(
            {
                "enabled": True,
                "port": port,
                "broadcast_host": "",
                "sysid": 250,
                "timeout_s": 5.0,
            }
        )
        is None
    )

    gcs = FakeRover(
        sysid=255,
        mav_type=mavlink2.MAV_TYPE_GCS,
        autopilot=mavlink2.MAV_AUTOPILOT_INVALID,
    )
    self_echo = FakeRover(
        sysid=250,
        mav_type=mavlink2.MAV_TYPE_QUADROTOR,
        autopilot=mavlink2.MAV_AUTOPILOT_ARDUPILOTMEGA,
    )
    gimbal = FakeRover(
        sysid=10,
        compid=154,
        mav_type=mavlink2.MAV_TYPE_GIMBAL,
        autopilot=mavlink2.MAV_AUTOPILOT_ARDUPILOTMEGA,
    )
    try:
        gcs.send_heartbeat(port)
        self_echo.send_heartbeat(port)
        gimbal.send_heartbeat(port)
        time.sleep(0.15)
        snap = mavlink.status_snapshot()
        assert snap["active_rovers"] == 0
        assert snap["rovers"] == []
    finally:
        gcs.shutdown()
        self_echo.shutdown()
        gimbal.shutdown()


def test_multi_rover_timeout_and_stale_pruning(monkeypatch):
    monkeypatch.setattr(mavlink, "STALE_RETENTION_S", 0.3)
    monkeypatch.setattr(mavlink, "VERSION_SLOW_RETRY_INTERVAL_S", 10.0)
    port = _free_udp_port()
    assert (
        mavlink.start(
            {
                "enabled": True,
                "port": port,
                "broadcast_host": "",
                "sysid": 250,
                "timeout_s": 0.25,
            }
        )
        is None
    )

    rover1 = FakeRover(sysid=1, auto_reply_version=False)
    rover2 = FakeRover(
        sysid=2,
        mav_type=mavlink2.MAV_TYPE_GROUND_ROVER,
        auto_reply_version=True,
    )
    try:
        rover1.send_heartbeat(port)
        rover2.send_heartbeat(port)
        assert _wait_until(lambda: mavlink.status_snapshot()["active_rovers"] == 2)

        frame1 = _make_rtcm_frame(120)
        mavlink.forward_mavlink(frame1)
        assert _wait_until(
            lambda: (
                len(rover1.reassembled_rtcm) == 1 and len(rover2.reassembled_rtcm) == 1
            )
        )

        # Wait for timeout (0.25s) while refreshing only rover2
        time.sleep(0.15)
        rover2.send_heartbeat(port)
        time.sleep(0.15)
        rover2.send_heartbeat(port)

        # Now rover1 is inactive (stale), rover2 is still active
        assert _wait_until(lambda: mavlink.status_snapshot()["active_rovers"] == 1)
        snap = mavlink.status_snapshot()
        by_id = {r["sysid"]: r for r in snap["rovers"]}
        assert by_id[1]["active"] is False
        assert by_id[2]["active"] is True

        # Verify confirmation starts at 0 and increments on fast retries
        assert [c.confirmation for c in rover1.received_commands] == list(
            range(len(rover1.received_commands))
        )
        assert len(rover1.received_commands) <= mavlink.VERSION_FAST_RETRIES

        # Stop heartbeating rover2 as well -> both expire and get pruned after STALE_RETENTION_S
        assert _wait_until(
            lambda: len(mavlink.status_snapshot()["rovers"]) == 0, timeout=2.0
        )
    finally:
        rover1.shutdown()
        rover2.shutdown()
