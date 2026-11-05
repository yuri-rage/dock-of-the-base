"""
MAVLink2 UDP transceiver for vehicle-gated RTCM3 correction injection.

Listens for vehicle heartbeats on UDP, queries AUTOPILOT_VERSION (msg 148),
monitors rover RTK fix status via GPS_RAW_INT (msg 24), emits a 1 Hz base
station heartbeat (sysid 250, compid 191, MAV_TYPE_ONBOARD_CONTROLLER), and
streams fragmented GPS_RTCM_DATA (msg 233) unicast to active rovers.

-- Yuri - Nov 2026
"""

import logging
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any

from pymavlink.dialects.v20 import common as mavlink2

log = logging.getLogger(__name__)

DEFAULT_PORT = 14550
DEFAULT_BROADCAST_HOST = ""
DEFAULT_SYSID = 250
DEFAULT_COMPID = mavlink2.MAV_COMP_ID_ONBOARD_COMPUTER  # 191
DEFAULT_TIMEOUT_S = 20.0
MIN_TIMEOUT_S = 5.0
MAX_TIMEOUT_S = 300.0
STALE_RETENTION_S = 60.0
MAX_ROVERS = 64

VERSION_FAST_RETRIES = 3
VERSION_RETRY_INTERVAL_S = 1.0
VERSION_SLOW_RETRY_INTERVAL_S = 30.0
HEARTBEAT_INTERVAL_S = 1.0

MAX_FRAG_LEN = 180
MAX_FRAGS = 4
MAX_RTCM_LEN = MAX_FRAG_LEN * MAX_FRAGS  # 720 bytes

type RoverKey = tuple[tuple[str, int], int]

GPS_FIX_NAMES: dict[int, str] = {
    0: "No GPS",
    1: "No Fix",
    2: "2D Fix",
    3: "3D Fix",
    4: "DGPS",
    5: "RTK Float",
    6: "RTK Fixed",
    7: "Static",
    8: "PPP",
}

ARDUPILOT_VEHICLE_NAMES: dict[int, str] = {
    mavlink2.MAV_TYPE_FIXED_WING: "Plane",
    mavlink2.MAV_TYPE_QUADROTOR: "Copter",
    mavlink2.MAV_TYPE_COAXIAL: "Copter",
    mavlink2.MAV_TYPE_HELICOPTER: "Copter",
    mavlink2.MAV_TYPE_ANTENNA_TRACKER: "Tracker",
    mavlink2.MAV_TYPE_GROUND_ROVER: "Rover",
    mavlink2.MAV_TYPE_SURFACE_BOAT: "Boat",
    mavlink2.MAV_TYPE_SUBMARINE: "Sub",
    mavlink2.MAV_TYPE_HEXAROTOR: "Copter",
    mavlink2.MAV_TYPE_OCTOROTOR: "Copter",
    mavlink2.MAV_TYPE_TRICOPTER: "Copter",
    mavlink2.MAV_TYPE_DECAROTOR: "Copter",
    mavlink2.MAV_TYPE_DODECAROTOR: "Copter",
    mavlink2.MAV_TYPE_GENERIC_MULTIROTOR: "Copter",
    mavlink2.MAV_TYPE_AIRSHIP: "Blimp",
    mavlink2.MAV_TYPE_VTOL_DUOROTOR: "Plane",
    mavlink2.MAV_TYPE_VTOL_QUADROTOR: "Plane",
    mavlink2.MAV_TYPE_VTOL_TILTROTOR: "Plane",
    mavlink2.MAV_TYPE_VTOL_RESERVED2: "Plane",
    mavlink2.MAV_TYPE_VTOL_RESERVED3: "Plane",
    mavlink2.MAV_TYPE_VTOL_RESERVED4: "Plane",
    mavlink2.MAV_TYPE_VTOL_RESERVED5: "Plane",
}

NON_VEHICLE_MAV_TYPES: set[int] = {
    mavlink2.MAV_TYPE_GCS,
    mavlink2.MAV_TYPE_ONBOARD_CONTROLLER,
    mavlink2.MAV_TYPE_GIMBAL,
    mavlink2.MAV_TYPE_ADSB,
    mavlink2.MAV_TYPE_CAMERA,
    mavlink2.MAV_TYPE_CHARGING_STATION,
    mavlink2.MAV_TYPE_SERVO,
    mavlink2.MAV_TYPE_ODID,
}


@dataclass
class RoverState:
    sysid: int
    compid: int
    addr: tuple[str, int]
    mav_type: int
    autopilot: int
    firmware_version: str | None = None
    git_hash: str | None = None
    fix_type: int | None = None
    satellites_visible: int | None = None
    h_acc_m: float | None = None
    last_heartbeat: float = 0.0
    version_requests: int = 0
    last_version_request: float = 0.0


def format_vehicle_label(
    autopilot: int,
    mav_type: int,
    firmware_version: str | None = None,
) -> str:
    """Render a concise vehicle description, e.g. 'Rover 4.8.0-dev'."""
    if autopilot == mavlink2.MAV_AUTOPILOT_ARDUPILOTMEGA:
        fw_name = ARDUPILOT_VEHICLE_NAMES.get(mav_type, "ArduPilot")
    elif autopilot == mavlink2.MAV_AUTOPILOT_PX4:
        fw_name = "PX4"
    else:
        ap_entry = mavlink2.enums.get("MAV_AUTOPILOT", {}).get(autopilot)
        if ap_entry:
            fw_name = (
                ap_entry.name.removeprefix("MAV_AUTOPILOT_").replace("_", " ").title()
            )
        else:
            fw_name = "Vehicle"

    return f"{fw_name} {firmware_version}" if firmware_version else fw_name


def decode_autopilot_version(
    flight_sw_version: int, flight_custom_version: list[int] | bytes
) -> tuple[str, str | None]:
    """Decode flight_sw_version and flight_custom_version from AUTOPILOT_VERSION."""
    major = (flight_sw_version >> 24) & 0xFF
    minor = (flight_sw_version >> 16) & 0xFF
    patch = (flight_sw_version >> 8) & 0xFF
    ver_type = flight_sw_version & 0xFF

    suffix_map = {
        0: "-dev",
        64: "-alpha",
        128: "-beta",
        192: "-rc",
        255: "",
    }
    suffix = suffix_map.get(ver_type, "")
    ver_str = f"{major}.{minor}.{patch}{suffix}"

    raw_custom = bytes(flight_custom_version).rstrip(b"\x00")
    git_hash: str | None = None
    if raw_custom:
        try:
            decoded = raw_custom.decode("ascii")
            if all(32 <= ord(c) < 127 for c in decoded):
                git_hash = decoded
        except UnicodeDecodeError:
            git_hash = raw_custom[:4].hex()

    return ver_str, git_hash


def fragment_rtcm3(raw: bytes, seq_id: int) -> list[tuple[int, int, list[int]]]:
    """
    Split a single RTCM3 frame into GPS_RTCM_DATA (flags, length, data_180) tuples.

    Follows ArduPilot AP_GPS::handle_gps_rtcm_fragment rules:
    - Up to 4 fragments of 180 bytes each (max 720 bytes).
    - Unfragmented (<= 180 bytes): bit 0 = 0, single packet.
    - Fragmented (181..720 bytes): bit 0 = 1, frag_id in bits 1..2, seq_id in bits 3..7.
    - Exact multiples of 180 where fragment count < 4 (360, 540) append a 0-length
      terminator fragment; 720 uses 4 full 180-byte fragments (completed via 0x0F mask).
    - Frames > 720 bytes exceed protocol capacity and return [].
    """
    n = len(raw)
    if n == 0 or n > MAX_RTCM_LEN:
        return []

    seq_bits = (seq_id & 0x1F) << 3

    if n <= MAX_FRAG_LEN:
        padded = list(raw) + [0] * (MAX_FRAG_LEN - n)
        return [(seq_bits, n, padded)]

    chunks = [raw[i : i + MAX_FRAG_LEN] for i in range(0, n, MAX_FRAG_LEN)]
    if n % MAX_FRAG_LEN == 0 and len(chunks) < MAX_FRAGS:
        chunks.append(b"")

    result: list[tuple[int, int, list[int]]] = []
    for frag_id, chunk in enumerate(chunks):
        flags = 1 | ((frag_id & 0x03) << 1) | seq_bits
        c_len = len(chunk)
        padded = list(chunk) + [0] * (MAX_FRAG_LEN - c_len)
        result.append((flags, c_len, padded))
    return result


# --- Module state ---

_lock = threading.Lock()
_lifecycle_lock = threading.Lock()
_sock: socket.socket | None = None
_active = False
_stop_event = threading.Event()
_rx_thread: threading.Thread | None = None
_hb_thread: threading.Thread | None = None
_gen = 0

_cfg_port: int = DEFAULT_PORT
_cfg_broadcast_host: str = DEFAULT_BROADCAST_HOST
_cfg_sysid: int = DEFAULT_SYSID
_cfg_timeout_s: float = DEFAULT_TIMEOUT_S

_seq_id: int = 0
_rovers: dict[RoverKey, RoverState] = {}

_rtcm_frames_sent: int = 0
_rtcm_fragments_sent: int = 0
_rtcm_dropped_oversize: int = 0
_rtcm_dropped_by_type: dict[int, int] = {}
_rtcm_drop_last_log: dict[int, float] = {}


def _make_mav(sysid: int) -> mavlink2.MAVLink:
    mav = mavlink2.MAVLink(None, srcSystem=sysid, srcComponent=DEFAULT_COMPID)
    mav.robust_parsing = True
    return mav


_tx_mav: mavlink2.MAVLink = _make_mav(DEFAULT_SYSID)


def _should_request_version(rover: RoverState, now: float) -> bool:
    """Return True if an AUTOPILOT_VERSION request is due for rover."""
    if rover.firmware_version is not None:
        return False
    if rover.version_requests == 0:
        return True
    interval = (
        VERSION_RETRY_INTERVAL_S
        if rover.version_requests < VERSION_FAST_RETRIES
        else VERSION_SLOW_RETRY_INTERVAL_S
    )
    return (now - rover.last_version_request) >= interval


def _pack_version_request(
    mav: mavlink2.MAVLink, rover: RoverState, now: float
) -> bytes:
    """
    Build and pack MAV_CMD_REQUEST_MESSAGE for AUTOPILOT_VERSION (148).
    Caller must hold _lock.
    """
    confirmation = min(rover.version_requests, 255)
    rover.version_requests += 1
    rover.last_version_request = now
    msg = mav.command_long_encode(
        rover.sysid,
        rover.compid,
        mavlink2.MAV_CMD_REQUEST_MESSAGE,
        confirmation,
        mavlink2.MAVLINK_MSG_ID_AUTOPILOT_VERSION,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    return msg.pack(mav)


def _handle_message(
    msg: Any,
    addr: tuple[str, int],
    now: float,
    sock: socket.socket,
    sysid: int,
) -> None:
    """Process a decoded MAVLink message from a remote peer."""
    msg_type = msg.get_type()
    if msg_type == "BAD_DATA":
        return

    src_sys = msg.get_srcSystem()
    src_comp = msg.get_srcComponent()

    if src_sys == sysid or src_sys == 0:
        return

    key: RoverKey = (addr, src_sys)

    if msg_type == "HEARTBEAT":
        if (
            msg.autopilot == mavlink2.MAV_AUTOPILOT_INVALID
            or msg.type in NON_VEHICLE_MAV_TYPES
            or src_comp != mavlink2.MAV_COMP_ID_AUTOPILOT1
        ):
            return

        req_pkt: bytes | None = None
        with _lock:
            rover = _rovers.get(key)
            if rover is None:
                if len(_rovers) >= MAX_ROVERS:
                    oldest_key = min(_rovers, key=lambda k: _rovers[k].last_heartbeat)
                    del _rovers[oldest_key]
                rover = RoverState(
                    sysid=src_sys,
                    compid=src_comp,
                    addr=addr,
                    mav_type=msg.type,
                    autopilot=msg.autopilot,
                    last_heartbeat=now,
                )
                _rovers[key] = rover
                log.info(
                    "MAVLink vehicle discovered: sysid=%d (%s) at %s:%d",
                    src_sys,
                    format_vehicle_label(msg.autopilot, msg.type),
                    addr[0],
                    addr[1],
                )
            else:
                was_inactive = (now - rover.last_heartbeat) > _cfg_timeout_s
                rover.mav_type = msg.type
                rover.autopilot = msg.autopilot
                rover.last_heartbeat = now
                if was_inactive and rover.firmware_version is None:
                    rover.version_requests = 0

            if _should_request_version(rover, now):
                req_pkt = _pack_version_request(_tx_mav, rover, now)

        if req_pkt is not None:
            try:
                sock.sendto(req_pkt, addr)
            except OSError:
                pass

    elif msg_type == "AUTOPILOT_VERSION":
        ver_str, git_hash = decode_autopilot_version(
            msg.flight_sw_version, msg.flight_custom_version
        )
        with _lock:
            rover = _rovers.get(key)
            if rover is not None:
                rover.firmware_version = ver_str
                rover.git_hash = git_hash
                log.info(
                    "MAVLink vehicle sysid=%d (%s:%d) firmware: %s",
                    src_sys,
                    addr[0],
                    addr[1],
                    format_vehicle_label(rover.autopilot, rover.mav_type, ver_str),
                )

    elif msg_type in ("GPS_RAW_INT", "GPS2_RAW"):
        with _lock:
            rover = _rovers.get(key)
            if rover is not None and (
                msg_type == "GPS_RAW_INT" or rover.fix_type is None
            ):
                rover.fix_type = msg.fix_type
                rover.satellites_visible = (
                    msg.satellites_visible if msg.satellites_visible != 255 else None
                )
                h_acc = getattr(msg, "h_acc", 0)
                if h_acc and h_acc > 0:
                    rover.h_acc_m = h_acc / 1000.0


def _rx_loop(
    sock: socket.socket,
    gen: int,
    stop_ev: threading.Event,
    sysid: int,
) -> None:
    """Receive incoming MAVLink UDP packets and update rover state."""
    parsers: dict[tuple[str, int], mavlink2.MAVLink] = {}
    while not stop_ev.is_set():
        try:
            data, addr = sock.recvfrom(4096)
        except TimeoutError:
            continue
        except OSError:
            if stop_ev.is_set():
                break
            continue

        if not data:
            continue

        with _lock:
            if gen != _gen or not _active:
                break

        parser = parsers.get(addr)
        if parser is None:
            if len(parsers) > MAX_ROVERS:
                parsers.clear()
            parser = _make_mav(sysid)
            parsers[addr] = parser

        now = time.monotonic()
        msgs: list[Any] | None = None
        try:
            msgs = parser.parse_buffer(data)
        except mavlink2.MAVError as exc:
            log.debug("MAVLink parse error from %s: %s", addr, exc)

        if msgs:
            for m in msgs:
                _handle_message(m, addr, now, sock, sysid)


def _hb_loop(
    sock: socket.socket,
    gen: int,
    stop_ev: threading.Event,
    broadcast_host: str,
    port: int,
    timeout_s: float,
) -> None:
    """Emit 1 Hz base station heartbeat, retry version requests, and prune expired rovers."""
    while not stop_ev.is_set():
        now = time.monotonic()
        retry_packets: list[tuple[tuple[str, int], bytes]] = []
        active_addrs: set[tuple[str, int]] = set()

        with _lock:
            if gen != _gen or not _active:
                break

            expired = [
                k
                for k, r in _rovers.items()
                if (now - r.last_heartbeat) > (timeout_s + STALE_RETENTION_S)
            ]
            for k in expired:
                del _rovers[k]

            for r in _rovers.values():
                if (now - r.last_heartbeat) <= timeout_s:
                    active_addrs.add(r.addr)
                    if _should_request_version(r, now):
                        pkt = _pack_version_request(_tx_mav, r, now)
                        retry_packets.append((r.addr, pkt))

            hb_msg = _tx_mav.heartbeat_encode(
                mavlink2.MAV_TYPE_ONBOARD_CONTROLLER,
                mavlink2.MAV_AUTOPILOT_INVALID,
                0,
                0,
                mavlink2.MAV_STATE_ACTIVE,
            )
            hb_pkt = hb_msg.pack(_tx_mav)

        targets: set[tuple[str, int]] = set(active_addrs)
        if broadcast_host.strip():
            targets.add((broadcast_host.strip(), port))

        for target in targets:
            try:
                sock.sendto(hb_pkt, target)
            except OSError:
                pass

        for addr, pkt in retry_packets:
            try:
                sock.sendto(pkt, addr)
            except OSError:
                pass

        if stop_ev.wait(HEARTBEAT_INTERVAL_S):
            break


def forward_mavlink(raw: bytes) -> None:
    """
    Fragment and unicast an RTCM3 frame to all currently active rovers.
    Called from receiver._reader_loop; never raises OSError.
    """
    global _seq_id, _rtcm_frames_sent, _rtcm_fragments_sent, _rtcm_dropped_oversize

    if len(raw) < 5 or raw[0] != 0xD3:
        return

    now = time.monotonic()
    should_log = False
    drop_count = 0
    msg_type = (raw[3] << 4) | (raw[4] >> 4)
    packets: list[bytes] = []

    with _lock:
        if not _active or _sock is None:
            return
        sock = _sock
        timeout_s = _cfg_timeout_s
        active_addrs = {
            r.addr for r in _rovers.values() if (now - r.last_heartbeat) <= timeout_s
        }
        if not active_addrs:
            return

        if len(raw) > MAX_RTCM_LEN:
            _rtcm_dropped_oversize += 1
            _rtcm_dropped_by_type[msg_type] = _rtcm_dropped_by_type.get(msg_type, 0) + 1
            drop_count = _rtcm_dropped_by_type[msg_type]
            last_log = _rtcm_drop_last_log.get(msg_type)
            if last_log is None or (now - last_log) >= 60.0:
                _rtcm_drop_last_log[msg_type] = now
                should_log = True
        else:
            seq = _seq_id
            _seq_id = (_seq_id + 1) & 0x1F
            tx_mav = _tx_mav

            frags = fragment_rtcm3(raw, seq)
            if not frags:
                return

            for flags, length, data_180 in frags:
                msg = tx_mav.gps_rtcm_data_encode(flags, length, data_180)
                packets.append(msg.pack(tx_mav))

            _rtcm_frames_sent += 1
            _rtcm_fragments_sent += len(packets)

    if should_log:
        log.warning(
            "MAVLink dropped oversized RTCM3 frame: type %d (%d bytes > %d max, %d dropped so far)",
            msg_type,
            len(raw),
            MAX_RTCM_LEN,
            drop_count,
        )

    if not packets:
        return

    for addr in active_addrs:
        for pkt in packets:
            try:
                sock.sendto(pkt, addr)
            except OSError:
                pass


def start(cfg: dict[str, Any] | None) -> str | None:
    """
    Start or restart the MAVLink UDP service from a mavlink config dict.
    Returns None on success or when disabled, or an error string if socket bind fails.
    If bind fails, the currently running service is preserved intact.
    """
    global _sock, _active, _stop_event, _rx_thread, _hb_thread, _gen
    global _cfg_port, _cfg_broadcast_host, _cfg_sysid, _cfg_timeout_s, _tx_mav
    global _seq_id, _rtcm_frames_sent, _rtcm_fragments_sent, _rtcm_dropped_oversize

    with _lifecycle_lock:
        if not cfg or not cfg.get("enabled", False):
            _stop_locked()
            return None

        try:
            port = int(cfg.get("port", DEFAULT_PORT))
            broadcast_host = str(
                cfg.get("broadcast_host", DEFAULT_BROADCAST_HOST)
            ).strip()
            sysid = int(cfg.get("sysid", DEFAULT_SYSID))
            timeout_s = float(cfg.get("timeout_s", DEFAULT_TIMEOUT_S))
        except (ValueError, TypeError) as exc:
            log.error("Invalid MAVLink configuration values: %s", exc)
            return f"Invalid MAVLink configuration: {exc}"

        if not (1 <= port <= 65535):
            return f"Invalid MAVLink UDP port: {port}"
        if not (1 <= sysid <= 254):
            return f"Invalid MAVLink sysid: {sysid} (must be 1–254)"
        if timeout_s <= 0:
            return "Invalid MAVLink timeout"
        timeout_s = max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, timeout_s))

        with _lock:
            current_active = _active
            current_port = _cfg_port

        if current_active and port == current_port:
            _stop_locked()

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(0.2)
            sock.bind(("0.0.0.0", port))
        except OSError as exc:
            log.error("Failed to bind MAVLink UDP port %d: %s", port, exc)
            return f"Failed to bind UDP port {port}: {exc}"

        with _lock:
            was_active = _active
        if was_active:
            _stop_locked()

        with _lock:
            _gen += 1
            gen = _gen
            _sock = sock
            _active = True
            _stop_event = threading.Event()
            stop_ev = _stop_event
            _cfg_port = port
            _cfg_broadcast_host = broadcast_host
            _cfg_sysid = sysid
            _cfg_timeout_s = timeout_s
            _tx_mav = _make_mav(sysid)
            _seq_id = 0
            _rovers.clear()
            _rtcm_frames_sent = 0
            _rtcm_fragments_sent = 0
            _rtcm_dropped_oversize = 0
            _rtcm_dropped_by_type.clear()
            _rtcm_drop_last_log.clear()

        _rx_thread = threading.Thread(
            target=_rx_loop,
            args=(sock, gen, stop_ev, sysid),
            daemon=True,
            name="mavlink-rx",
        )
        _hb_thread = threading.Thread(
            target=_hb_loop,
            args=(sock, gen, stop_ev, broadcast_host, port, timeout_s),
            daemon=True,
            name="mavlink-hb",
        )
        _rx_thread.start()
        _hb_thread.start()
        log.info(
            "MAVLink service started on UDP :%d (sysid=%d, compid=%d, broadcast=%s, timeout=%.0fs)",
            port,
            sysid,
            DEFAULT_COMPID,
            broadcast_host or "none",
            timeout_s,
        )
        return None


def _stop_locked() -> None:
    """Internal helper to stop service. Caller must hold _lifecycle_lock."""
    global _sock, _active
    with _lock:
        _active = False
        _stop_event.set()
        sock = _sock
        _sock = None
        _rovers.clear()

    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass

    if _rx_thread and _rx_thread.is_alive():
        _rx_thread.join(timeout=1.0)
    if _hb_thread and _hb_thread.is_alive():
        _hb_thread.join(timeout=1.0)


def stop() -> None:
    """Stop the MAVLink UDP service and close the socket."""
    with _lifecycle_lock:
        _stop_locked()


def status_snapshot() -> dict[str, Any]:
    """Return a thread-safe snapshot of MAVLink state and rover table for the UI."""
    now = time.monotonic()
    with _lock:
        enabled = _active
        port = _cfg_port if _active else None
        sysid = _cfg_sysid
        timeout_s = _cfg_timeout_s
        frames_sent = _rtcm_frames_sent
        frags_sent = _rtcm_fragments_sent
        dropped = _rtcm_dropped_oversize
        dropped_by_type = dict(sorted(_rtcm_dropped_by_type.items()))

        rovers_list: list[dict[str, Any]] = []
        active_count = 0
        sorted_rovers = sorted(
            _rovers.values(), key=lambda r: (r.sysid, r.addr[0], r.addr[1])
        )
        for r in sorted_rovers:
            age_s = max(0.0, now - r.last_heartbeat)
            is_active = age_s <= timeout_s
            if is_active:
                active_count += 1
            rovers_list.append(
                {
                    "sysid": r.sysid,
                    "compid": r.compid,
                    "endpoint": f"{r.addr[0]}:{r.addr[1]}",
                    "label": format_vehicle_label(
                        r.autopilot, r.mav_type, r.firmware_version
                    ),
                    "firmware_version": r.firmware_version,
                    "fix_type": r.fix_type,
                    "fix_name": GPS_FIX_NAMES.get(r.fix_type, "—")
                    if r.fix_type is not None
                    else "—",
                    "satellites": r.satellites_visible,
                    "h_acc_m": r.h_acc_m,
                    "age_s": age_s,
                    "active": is_active,
                }
            )

    return {
        "enabled": enabled,
        "port": port,
        "sysid": sysid,
        "timeout_s": timeout_s,
        "active_rovers": active_count,
        "streaming": enabled and active_count > 0,
        "rovers": rovers_list,
        "rtcm_frames_sent": frames_sent,
        "rtcm_fragments_sent": frags_sent,
        "rtcm_dropped_oversize": dropped,
        "rtcm_dropped_by_type": dropped_by_type,
    }
