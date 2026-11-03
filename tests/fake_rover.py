"""
Fake MAVLink rover fixture for testing app.mavlink over localhost UDP.

Simulates an ArduPilot vehicle that:
- Sends HEARTBEAT and GPS_RAW_INT packets to the base station
- Listens for COMMAND_LONG (MAV_CMD_REQUEST_MESSAGE for AUTOPILOT_VERSION)
  and optionally replies with AUTOPILOT_VERSION
- Reassembles incoming GPS_RTCM_DATA (msg 233) fragments using ArduPilot's
  AP_GPS::handle_gps_rtcm_fragment state machine
"""

from __future__ import annotations

import socket
import threading
from typing import Any

from pymavlink.dialects.v20 import common as mavlink2

MAX_FRAG_LEN = 180
MAX_FRAGS = 4


class FakeRover:
    def __init__(
        self,
        sysid: int = 1,
        compid: int = mavlink2.MAV_COMP_ID_AUTOPILOT1,
        mav_type: int = mavlink2.MAV_TYPE_QUADROTOR,
        autopilot: int = mavlink2.MAV_AUTOPILOT_ARDUPILOTMEGA,
        auto_reply_version: bool = True,
        sw_version: tuple[int, int, int, int] = (4, 6, 2, 255),
        git_hash: bytes = b"a1b2c3d4",
    ) -> None:
        self.sysid = sysid
        self.compid = compid
        self.mav_type = mav_type
        self.autopilot = autopilot
        self.auto_reply_version = auto_reply_version
        self.sw_version = sw_version
        self.git_hash = git_hash

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(0.1)
        self._sock.bind(("127.0.0.1", 0))
        self.port: int = self._sock.getsockname()[1]

        self._mav = mavlink2.MAVLink(
            None, srcSystem=self.sysid, srcComponent=self.compid
        )
        self._mav.robust_parsing = True
        self._rx_mav = mavlink2.MAVLink(None)
        self._rx_mav.robust_parsing = True

        self._lock = threading.Lock()
        self._stop = threading.Event()

        self.received_heartbeats: list[Any] = []
        self.received_commands: list[Any] = []
        self.received_rtcm_fragments: list[Any] = []
        self.reassembled_rtcm: list[bytes] = []

        # ArduPilot AP_GPS::rtcm_buffer state
        self._rtcm_buffer = bytearray(MAX_FRAG_LEN * MAX_FRAGS)
        self._rtcm_fragments_received: int = 0
        self._rtcm_sequence: int = 0
        self._rtcm_fragment_count: int = 0
        self._rtcm_total_length: int = 0

        self._thread = threading.Thread(
            target=self._rx_loop, daemon=True, name=f"fake-rover-{sysid}-{self.port}"
        )
        self._thread.start()

    def _reset_rtcm_buffer(self) -> None:
        self._rtcm_buffer[:] = b"\x00" * (MAX_FRAG_LEN * MAX_FRAGS)
        self._rtcm_fragments_received = 0
        self._rtcm_sequence = 0
        self._rtcm_fragment_count = 0
        self._rtcm_total_length = 0

    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                break

            msgs: list[Any] | None = None
            try:
                msgs = self._rx_mav.parse_buffer(data)
            except mavlink2.MAVError:
                continue

            if not msgs:
                continue

            for m in msgs:
                mtype = m.get_type()
                if mtype == "HEARTBEAT":
                    with self._lock:
                        self.received_heartbeats.append(m)
                elif mtype == "COMMAND_LONG":
                    with self._lock:
                        self.received_commands.append(m)
                    if (
                        self.auto_reply_version
                        and m.command == mavlink2.MAV_CMD_REQUEST_MESSAGE
                        and int(m.param1) == mavlink2.MAVLINK_MSG_ID_AUTOPILOT_VERSION
                    ):
                        self.send_autopilot_version(addr[1])
                elif mtype == "GPS_RTCM_DATA":
                    self.handle_rtcm_fragment(m)

    def handle_rtcm_fragment(self, m: Any) -> None:
        """Port of ArduPilot's AP_GPS::handle_gps_rtcm_fragment."""
        flags = int(m.flags)
        length = int(m.len)
        if length > MAX_FRAG_LEN:
            return
        payload = bytes(m.data[:length])

        with self._lock:
            self.received_rtcm_fragments.append(m)

            if (flags & 1) == 0:
                self.reassembled_rtcm.append(payload)
                return

            fragment = (flags >> 1) & 0x03
            sequence = (flags >> 3) & 0x1F
            offset = MAX_FRAG_LEN * fragment

            if self._rtcm_fragments_received != 0:
                seq_changed = self._rtcm_sequence != sequence
                seen_frag = (self._rtcm_fragments_received & (1 << fragment)) != 0
                if seq_changed or seen_frag:
                    if (
                        not seq_changed
                        and seen_frag
                        and bytes(self._rtcm_buffer[offset : offset + length])
                        == payload
                    ):
                        # Duplicate fragment: ignore
                        return
                    self._reset_rtcm_buffer()

            self._rtcm_sequence = sequence
            self._rtcm_fragments_received |= 1 << fragment
            self._rtcm_buffer[offset : offset + length] = payload

            if length < MAX_FRAG_LEN:
                self._rtcm_fragment_count = fragment + 1
                self._rtcm_total_length = (MAX_FRAG_LEN * fragment) + length
            elif self._rtcm_fragments_received == 0x0F:
                self._rtcm_fragment_count = MAX_FRAGS
                self._rtcm_total_length = MAX_FRAG_LEN * MAX_FRAGS

            if (
                self._rtcm_fragment_count != 0
                and self._rtcm_fragments_received
                == (1 << self._rtcm_fragment_count) - 1
            ):
                self.reassembled_rtcm.append(
                    bytes(self._rtcm_buffer[: self._rtcm_total_length])
                )
                self._reset_rtcm_buffer()

    def send_heartbeat(self, target_port: int) -> None:
        msg = self._mav.heartbeat_encode(
            self.mav_type,
            self.autopilot,
            0,
            0,
            mavlink2.MAV_STATE_ACTIVE,
        )
        self._sock.sendto(msg.pack(self._mav), ("127.0.0.1", target_port))

    def send_autopilot_version(self, target_port: int) -> None:
        maj, min_v, pat, vtype = self.sw_version
        flight_sw = (
            ((maj & 0xFF) << 24)
            | ((min_v & 0xFF) << 16)
            | ((pat & 0xFF) << 8)
            | (vtype & 0xFF)
        )
        custom = list(self.git_hash[:8].ljust(8, b"\x00"))
        zeros8 = [0] * 8
        msg = self._mav.autopilot_version_encode(
            0,
            flight_sw,
            0,
            0,
            0,
            custom,
            zeros8,
            zeros8,
            0,
            0,
            0,
        )
        self._sock.sendto(msg.pack(self._mav), ("127.0.0.1", target_port))

    def send_gps_raw_int(
        self,
        target_port: int,
        fix_type: int = 6,
        satellites_visible: int = 18,
        h_acc_mm: int = 14,
    ) -> None:
        msg = self._mav.gps_raw_int_encode(
            0,
            fix_type,
            330198000,
            -966989000,
            200000,
            100,
            100,
            0,
            0,
            satellites_visible,
            0,
            h_acc_mm,
        )
        self._sock.sendto(msg.pack(self._mav), ("127.0.0.1", target_port))

    def shutdown(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)
