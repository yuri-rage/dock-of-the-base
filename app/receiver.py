"""
Persistent u-Blox receiver connection and state reader.

-- Yuri - May 2026
"""

import base64
import json
import logging
import re
import socket
import socketserver
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Any, cast
from urllib.parse import urlparse

from pyrtcm import RTCMParseError
from pyubx2 import POLL, UBXMessage, UBXMessageError, UBXParseError, UBXReader
from serial import Serial, SerialException

from app.ubx_cfg_valset import auto_baud_connect, config_fixed_ecef

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.json"
LOG_DIR = Path(__file__).parent.parent / "logs"
RECONNECT_INTERVAL = 15.0
NTRIP_IN_RECONNECT_INTERVAL = 30.0
NTRIP_OUT_RECONNECT_INITIAL = 5.0
NTRIP_OUT_RECONNECT_MAX = 60.0
DEFAULT_TTY_EXCLUDE = r"^tty(\d+|S\d+)?$"

FIX_TYPES = {
    0: "No fix",
    1: "Dead reckoning",
    2: "2D-fix",
    3: "3D-fix",
    4: "GNSS + DR",
    5: "Time only fix",
}

CARRIER_SOLUTION_TYPES = {
    0: "—",
    1: "FLOAT",
    2: "FIXED",
}

TMODE_NAMES = {
    0: "Disabled",
    1: "Survey-In",
    2: "Fixed",
}


@dataclass
class ReceiverState:
    connected: bool = False
    port: str | None = None
    tmode: int | None = None
    fix_type: int | None = None
    carrier_solution: int | None = None
    lat: float | None = None
    lon: float | None = None
    height_m: float | None = None
    height_msl_m: float | None = None
    h_acc_m: float | None = None
    v_acc_m: float | None = None
    hdop: float | None = None
    vdop: float | None = None
    pdop: float | None = None
    svin_active: bool = False
    svin_valid: bool = False
    svin_obs: int = 0
    svin_mean_acc_m: float | None = None
    svin_ecef_x: float | None = None
    svin_ecef_y: float | None = None
    svin_ecef_z: float | None = None
    num_sv: int = 0
    is_multiband: bool = False


state = ReceiverState()
_lock = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None

# TCP bridge state
_tcp_clients: set[socket.socket] = set()
_tcp_lock = threading.Lock()
_tcp_server: socketserver.TCPServer | None = None
_serial: Serial | None = None  # current open serial stream

# NTRIP caster state
_ntrip_clients: set[socket.socket] = set()
_ntrip_lock = threading.Lock()
_ntrip_server: socketserver.TCPServer | None = None
_ntrip_mount: str = "BASE"

# NTRIP client (corrections input) state
_ntrip_in_sock: socket.socket | None = None
_ntrip_in_thread: threading.Thread | None = None
_ntrip_in_active: bool = False
_ntrip_in_status: str = "disconnected"

# External NTRIP caster (output) state
_ntrip_out_sock: socket.socket | None = None
_ntrip_out_lock = threading.Lock()
_ntrip_out_active: bool = False
_ntrip_out_thread: threading.Thread | None = None
_ntrip_out_status: str = "disconnected"

# Raw binary log state
_log_file: IO[bytes] | None = None
_log_lock = threading.Lock()
_log_rotate_timer: threading.Timer | None = None

# Live config ACK synchronization
_ack_event = threading.Event()
_ack_ok: bool = False
_config_lock = threading.Lock()

_auto_fixed_pending: bool = False
_CONFIG_TIMEOUT = 1.0


def load_config() -> dict | None:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return None


def save_config(serial_port: str, port_type: str, target_baud: int) -> None:
    cfg = load_config() or {}
    cfg.update(
        {"serial_port": serial_port, "port_type": port_type, "target_baud": target_baud}
    )
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def save_last_configure(data: dict) -> None:
    cfg = load_config() or {}
    cfg["last_configure"] = data
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def tcp_client_count() -> int:
    with _tcp_lock:
        return len(_tcp_clients)


def tcp_port() -> int | None:
    return _tcp_server.server_address[1] if _tcp_server else None


def ntrip_client_count() -> int:
    with _ntrip_lock:
        return len(_ntrip_clients)


def ntrip_port() -> int | None:
    return _ntrip_server.server_address[1] if _ntrip_server else None


def ntrip_mount() -> str:
    return _ntrip_mount


def ntrip_in_connected() -> bool | None:
    """Returns True/False when ntrip_in is configured, None when unconfigured."""
    if not (load_config() or {}).get("ntrip_in"):
        return None
    return _ntrip_in_sock is not None


def ntrip_in_active() -> bool:
    """Returns True when the user has requested NTRIP-in (connected or reconnecting)."""
    return _ntrip_in_active


def ntrip_in_status_str() -> str | None:
    """Returns detailed status string when ntrip_in is configured, None when unconfigured."""
    if not (load_config() or {}).get("ntrip_in"):
        return None
    return _ntrip_in_status


def ntrip_out_connected() -> bool | None:
    """Returns True/False when external_caster is configured, None when unconfigured."""
    if not (load_config() or {}).get("external_caster"):
        return None
    with _ntrip_out_lock:
        return _ntrip_out_sock is not None


def ntrip_out_status_str() -> str | None:
    """Returns detailed status string when external_caster is configured, None when unconfigured."""
    if not (load_config() or {}).get("external_caster"):
        return None
    return _ntrip_out_status


# --- TCP bridge ---


def _forward_tcp(data: bytes) -> None:
    with _tcp_lock:
        dead: set[socket.socket] = set()
        for client in _tcp_clients:
            try:
                client.sendall(data)
            except OSError:
                dead.add(client)
        _tcp_clients.difference_update(dead)


class _TeeStream:
    """Wraps a Serial, forwarding received bytes to TCP clients."""

    def __init__(self, stream: Serial) -> None:
        self._s = stream

    @property
    def in_waiting(self) -> int:
        return self._s.in_waiting

    @property
    def timeout(self) -> float | None:
        return self._s.timeout

    @timeout.setter
    def timeout(self, value: float) -> None:
        self._s.timeout = value

    def read(self, n: int = 1) -> bytes:
        data = self._s.read(n)
        if data:
            _forward_tcp(data)
            _log_write(data)
        return data

    def readline(self) -> bytes:
        data = self._s.readline()
        if data:
            _forward_tcp(data)
            _log_write(data)
        return data

    def write(self, data: bytes) -> int | None:
        return self._s.write(data)

    def close(self) -> None:
        self._s.close()

    @property
    def is_open(self) -> bool:
        return self._s.is_open


class _ClientHandler(socketserver.BaseRequestHandler):
    def setup(self) -> None:
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with _tcp_lock:
            _tcp_clients.add(self.request)

    def handle(self) -> None:
        while not _stop.is_set():
            try:
                data = self.request.recv(4096)
                if not data:
                    break
                if _serial and _serial.is_open:
                    _serial.write(data)
            except OSError:
                break

    def finish(self) -> None:
        with _tcp_lock:
            _tcp_clients.discard(self.request)
        try:
            self.request.close()
        except OSError:
            pass


class _TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _start_tcp_server(port: int) -> socketserver.TCPServer | None:
    try:
        server = _TCPServer(("0.0.0.0", port), _ClientHandler)
        threading.Thread(
            target=server.serve_forever, daemon=True, name="tcp-bridge"
        ).start()
        log.info("TCP bridge started on port %d", port)
        return server
    except OSError as e:
        log.error("TCP bridge failed to start on port %d: %s", port, e)
        return None


def _stop_tcp_server() -> None:
    global _tcp_server
    if _tcp_server:
        _tcp_server.shutdown()
        _tcp_server.server_close()
        _tcp_server = None
    with _tcp_lock:
        for client in _tcp_clients:
            try:
                client.close()
            except OSError:
                pass
        _tcp_clients.clear()


# --- NTRIP caster ---


def _forward_ntrip(data: bytes) -> None:
    with _ntrip_lock:
        dead: set[socket.socket] = set()
        for client in _ntrip_clients:
            try:
                client.sendall(data)
            except OSError:
                dead.add(client)
        _ntrip_clients.difference_update(dead)


def _forward_ntrip_out(data: bytes) -> None:
    with _ntrip_out_lock:
        sock = _ntrip_out_sock
    if sock is None:
        return
    try:
        sock.sendall(data)
    except OSError:
        pass  # _ntrip_out_run thread detects the disconnect


def _build_source_table() -> bytes:
    with _lock:
        lat = state.lat or 0.0
        lon = state.lon or 0.0
    entry = (
        f"STR;{_ntrip_mount};{_ntrip_mount};RTCM 3.3;"
        "1005(5),1074(1),1084(1),1094(1),1124(1),1230(5);"
        f"2;GPS+GLO+GAL+BDS;;;{lat:.4f};{lon:.4f};0;1;"
        "dock-of-the-base;none;N;N;0;\r\n"
        "ENDSOURCETABLE\r\n"
    )
    return entry.encode()


def _parse_ntrip_headers(buf: bytes) -> tuple[str, str, dict[str, str]]:
    """Return (method, path, headers) from a buffered HTTP request."""
    lines = buf.split(b"\r\n")
    parts = lines[0].decode(errors="ignore").split()
    method = parts[0] if parts else ""
    path = parts[1].lstrip("/") if len(parts) > 1 else ""
    headers: dict[str, str] = {}
    for line in lines[1:]:
        decoded = line.decode(errors="ignore")
        if ":" in decoded:
            k, _, v = decoded.partition(":")
            headers[k.strip().lower()] = v.strip()
    return method, path, headers


def _ntrip_send_sourcetable(sock: socket.socket, ntrip2: bool) -> None:
    body = _build_source_table()
    if ntrip2:
        sock.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"Ntrip-Version: Ntrip/2.0\r\n"
            b"Cache-Control: no-store, no-cache, max-age=0\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
    else:
        sock.sendall(
            b"SOURCETABLE 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )


def _ntrip_stream_client(sock: socket.socket, ntrip2: bool) -> None:
    if ntrip2:
        sock.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: gnss/data\r\n"
            b"Ntrip-Version: Ntrip/2.0\r\n"
            b"Cache-Control: no-store, no-cache, max-age=0\r\n"
            b"\r\n"
        )
    else:
        sock.sendall(b"ICY 200 OK\r\n\r\n")
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.settimeout(1.0)
    with _ntrip_lock:
        _ntrip_clients.add(sock)
    try:
        while not _stop.is_set():
            try:
                if not sock.recv(256):  # rover GGA or disconnect
                    break
            except TimeoutError:
                continue
            except OSError:
                break
    finally:
        with _ntrip_lock:
            _ntrip_clients.discard(sock)


class _NTRIPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        try:
            self.request.settimeout(5.0)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = self.request.recv(1024)
                if not chunk:
                    return
                buf += chunk

            method, path, headers = _parse_ntrip_headers(buf)
            if method != "GET":
                self.request.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return

            ntrip2 = "ntrip/2.0" in headers.get("ntrip-version", "").lower()

            if not path:
                _ntrip_send_sourcetable(self.request, ntrip2)
                return

            if path.upper() != _ntrip_mount.upper():
                self.request.sendall(
                    b"HTTP/1.1 401 Unauthorized\r\n\r\n"
                    if ntrip2
                    else b"ICY 401 Unauthorized\r\n\r\n"
                )
                return

            _ntrip_stream_client(self.request, ntrip2)
        except OSError:
            pass


class _NTRIPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _start_ntrip_server(port: int, mount: str) -> socketserver.TCPServer | None:
    global _ntrip_mount
    _ntrip_mount = mount
    try:
        server = _NTRIPServer(("0.0.0.0", port), _NTRIPHandler)
        threading.Thread(
            target=server.serve_forever, daemon=True, name="ntrip-caster"
        ).start()
        log.info("NTRIP caster started on port %d, mount /%s", port, mount)
        return server
    except OSError as e:
        log.error("NTRIP caster failed to start on port %d: %s", port, e)
        return None


def _stop_ntrip_server() -> None:
    global _ntrip_server
    if _ntrip_server:
        _ntrip_server.shutdown()
        _ntrip_server.server_close()
        _ntrip_server = None
    with _ntrip_lock:
        for client in _ntrip_clients:
            try:
                client.close()
            except OSError:
                pass
        _ntrip_clients.clear()


# --- Receiver state updates ---


def _handle_mon_ver(msg: Any) -> None:
    multiband = False
    for i in range(1, 31):
        ext = getattr(msg, f"extension_{i:02d}", None)
        if ext is None:
            break
        if isinstance(ext, bytes):
            ext = ext.decode(errors="ignore")
        if "X20" in ext:
            multiband = True
            break
    state.is_multiband = multiband


def _handle_nav_pvt(msg: Any) -> None:
    state.fix_type = int(msg.fixType)
    state.carrier_solution = int(msg.carrSoln)
    state.lat = float(msg.lat)
    state.lon = float(msg.lon)
    state.height_m = float(msg.height) / 1000
    state.height_msl_m = float(msg.hMSL) / 1000
    state.h_acc_m = float(msg.hAcc) / 1000
    state.v_acc_m = float(msg.vAcc) / 1000
    state.num_sv = int(msg.numSV)


def _handle_nav_svin(msg: Any) -> None:
    global _auto_fixed_pending
    was_active = state.svin_active
    state.svin_active = bool(msg.active)
    state.svin_valid = bool(msg.valid)
    state.svin_obs = int(msg.obs)
    state.svin_mean_acc_m = float(msg.meanAcc) / 10000
    if state.svin_active:
        state.tmode = 1
        state.svin_ecef_x = int(msg.meanX) * 0.01 + int(msg.meanXHP) * 0.0001
        state.svin_ecef_y = int(msg.meanY) * 0.01 + int(msg.meanYHP) * 0.0001
        state.svin_ecef_z = int(msg.meanZ) * 0.01 + int(msg.meanZHP) * 0.0001
    elif was_active and state.svin_valid:
        _auto_fixed_pending = True


def _handle_nav_dop(msg: Any) -> None:
    state.pdop = float(msg.pDOP)
    state.vdop = float(msg.vDOP)
    state.hdop = float(msg.hDOP)


def _handle_cfg_valget(msg: Any) -> None:
    val = getattr(msg, "CFG_TMODE_MODE", None)
    if val is not None:
        state.tmode = int(val)


_MSG_HANDLERS: dict[str, Any] = {
    "MON-VER": _handle_mon_ver,
    "NAV-PVT": _handle_nav_pvt,
    "NAV-SVIN": _handle_nav_svin,
    "NAV-DOP": _handle_nav_dop,
    "CFG-VALGET": _handle_cfg_valget,
}


def _update(msg: Any) -> None:
    global _ack_ok
    if msg.identity == "ACK-ACK":
        _ack_ok = True
        _ack_event.set()
        return
    if msg.identity == "ACK-NAK":
        _ack_ok = False
        _ack_event.set()
        return
    handler = _MSG_HANDLERS.get(msg.identity)
    if handler:
        with _lock:
            handler(msg)


def _reset_nav() -> None:
    global _serial
    with _lock:
        state.connected = False
        state.is_multiband = False
        state.fix_type = None
        state.carrier_solution = None
        state.lat = None
        state.lon = None
        state.height_m = None
        state.height_msl_m = None
        state.h_acc_m = None
        state.v_acc_m = None
        state.svin_active = False
        state.svin_valid = False
        state.svin_mean_acc_m = None
        state.svin_ecef_x = None
        state.svin_ecef_y = None
        state.svin_ecef_z = None
        state.num_sv = 0
    _serial = None


def _apply_auto_fixed() -> None:
    """Persist surveyed position as Fixed mode to BBR/Flash after survey-in completes."""
    global _auto_fixed_pending
    _auto_fixed_pending = False
    with _lock:
        x, y, z = state.svin_ecef_x, state.svin_ecef_y, state.svin_ecef_z
        acc_m = state.svin_mean_acc_m
    if x is None or y is None or z is None or acc_m is None:
        return
    acc_limit = int(acc_m * 10000)  # metres → 0.1 mm units
    if not send_config(config_fixed_ecef(acc_limit, x, y, z)):
        log.warning("Auto-fixed: failed to persist Fixed mode after survey-in")
        return
    with _lock:
        state.tmode = 2
        state.svin_active = False
    cfg = load_config() or {}
    last = cfg.get("last_configure", {})
    last.update(
        {
            "tmode": 2,
            "coord_type": "ecef",
            "ecef_x": x,
            "ecef_y": y,
            "ecef_z": z,
            "acc_limit": acc_m,
        }
    )
    save_last_configure(last)
    log.info(
        "Auto-fixed: X=%.4f Y=%.4f Z=%.4f acc=%.4f m persisted to BBR/Flash",
        x,
        y,
        z,
        acc_m,
    )


def _reader_loop(stream: _TeeStream) -> None:
    reader = UBXReader(stream, protfilter=6, quitonerror=0)  # UBX + RTCM
    while not _stop.is_set():
        try:
            if stream.in_waiting:
                raw, msg = reader.read()
                if msg:
                    if isinstance(msg, UBXMessage):
                        _update(msg)
                        if _auto_fixed_pending:
                            threading.Thread(
                                target=_apply_auto_fixed,
                                daemon=True,
                                name="auto-fixed",
                            ).start()
                    elif raw:
                        _forward_ntrip(raw)
                        _forward_ntrip_out(raw)
            else:
                _stop.wait(0.01)
        except (UBXParseError, UBXMessageError, RTCMParseError):
            pass
        except (SerialException, OSError):
            break


def _connection_loop() -> None:
    global _serial
    while not _stop.is_set():
        cfg = load_config()
        if not cfg or "serial_port" not in cfg:
            _stop.wait(RECONNECT_INTERVAL)
            continue

        stream, _ = auto_baud_connect(
            cfg["serial_port"], cfg["port_type"], cfg["target_baud"]
        )

        if stream:
            _serial = stream
            with _lock:
                state.connected = True
                state.port = cfg["serial_port"]
                state.tmode = None
            log.info("Serial connected: %s", cfg["serial_port"])

            try:
                poll = cast(
                    UBXMessage, UBXMessage.config_poll(0, 0, ["CFG_TMODE_MODE"])
                )
                stream.write(poll.serialize())
                stream.write(UBXMessage("MON", "MON-VER", POLL).serialize())
            except Exception:
                pass

            _reader_loop(_TeeStream(stream))
            stream.close()

        _reset_nav()

        if not _stop.is_set():
            log.info(
                "Serial disconnected: %s — reconnecting in %.0fs",
                cfg["serial_port"],
                RECONNECT_INTERVAL,
            )
            _stop.wait(RECONNECT_INTERVAL)


# --- NTRIP corrections input ---


def _ntrip_in_wanted() -> bool:
    """False only when receiver is in known Fixed mode."""
    with _lock:
        return state.tmode != 2


def _ntrip_in_connect(cfg: dict) -> socket.socket | None:
    url = cfg.get("url", "")
    port = int(cfg.get("port", 2101))
    mount = cfg.get("mount_point", "")
    username = cfg.get("username", "")
    password = cfg.get("password", "")

    parsed = urlparse(url if "://" in url else "http://" + url)
    host = parsed.hostname or url
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    request = (
        f"GET /{mount} HTTP/1.0\r\n"
        f"User-Agent: NTRIP dock-of-the-base/1.0\r\n"
        f"Host: {host}\r\n"
        f"Authorization: Basic {credentials}\r\n"
        f"\r\n"
    )
    try:
        sock = socket.create_connection((host, port), timeout=10.0)
        sock.settimeout(10.0)
        sock.sendall(request.encode())
        buf = b""
        while len(buf) < 4096:
            chunk = sock.recv(256)
            if not chunk:
                break
            buf += chunk
            if b"\r\n" not in buf:
                continue
            # NTRIP 1.0: "ICY 200 OK\r\n" with no following headers
            if buf.startswith(b"ICY "):
                break
            # HTTP: wait for end of headers
            if b"\r\n\r\n" in buf:
                break
        first_line = buf.split(b"\r\n")[0].decode(errors="ignore")
        if "200" not in first_line:
            log.warning("NTRIP-in: rejected — %s", first_line)
            sock.close()
            return None
        log.info("NTRIP-in: connected to %s/%s", host, mount)
        sock.settimeout(5.0)
        return sock
    except OSError as e:
        log.warning("NTRIP-in: connection failed — %s", e)
        return None


def _ntrip_in_recv(sock: socket.socket) -> bool:
    """Receive RTCM corrections, forwarding to serial. Returns True if server dropped (reconnect), False to stop."""
    global _ntrip_in_active
    while not _stop.is_set() and _ntrip_in_active:
        if not _ntrip_in_wanted():
            _ntrip_in_active = False
            log.info("NTRIP-in: disconnecting — receiver entered Fixed mode")
            return False
        try:
            data = sock.recv(4096)
            if not data:
                log.warning("NTRIP-in: server closed connection")
                return True
            if _serial and _serial.is_open:
                _serial.write(data)
        except TimeoutError:
            continue
        except OSError as e:
            if _ntrip_in_active:
                log.warning("NTRIP-in: socket error: %s", e)
            return True
    return False


def _ntrip_in_loop(cfg: dict, initial_sock: socket.socket) -> None:
    global _ntrip_in_sock, _ntrip_in_status
    sock: socket.socket | None = initial_sock
    try:
        while not _stop.is_set() and _ntrip_in_active:
            if sock is None:
                sock = _ntrip_in_connect(cfg)
                if sock is None:
                    _stop.wait(NTRIP_IN_RECONNECT_INTERVAL)
                    continue
            _ntrip_in_sock = sock
            _ntrip_in_status = "connected"
            try:
                reconnect = _ntrip_in_recv(sock)
            finally:
                _ntrip_in_sock = None
                try:
                    sock.close()
                except OSError:
                    pass
                sock = None
            if not reconnect:
                break
            if not _stop.is_set():
                _ntrip_in_status = "reconnecting"
                log.info("NTRIP-in: reconnecting in %.0fs", NTRIP_IN_RECONNECT_INTERVAL)
                _stop.wait(NTRIP_IN_RECONNECT_INTERVAL)
    finally:
        _ntrip_in_status = "disconnected"
        log.info("NTRIP-in: disconnected")


def ntrip_in_connect() -> str | None:
    """Connect to NTRIP source synchronously. Returns an error string on failure, None on success."""
    global _ntrip_in_active, _ntrip_in_thread, _ntrip_in_status
    with _lock:
        tmode = state.tmode
    if tmode == 2:
        return "NTRIP input is not applicable in Fixed mode."
    ntrip_in = (load_config() or {}).get("ntrip_in")
    if not ntrip_in:
        return "NTRIP input is not configured."
    sock = _ntrip_in_connect(ntrip_in)
    if not sock:
        return "Failed to connect to NTRIP source."
    _ntrip_in_active = True
    _ntrip_in_status = "connected"
    _ntrip_in_thread = threading.Thread(
        target=_ntrip_in_loop, args=(ntrip_in, sock), daemon=True, name="ntrip-in"
    )
    _ntrip_in_thread.start()
    return None


def ntrip_in_disconnect() -> None:
    global _ntrip_in_active, _ntrip_in_status
    _ntrip_in_active = False
    _ntrip_in_status = "disconnected"
    if _ntrip_in_sock:
        try:
            _ntrip_in_sock.close()
        except OSError:
            pass


# --- External NTRIP caster (output) ---


def _ntrip_out_connect(cfg: dict) -> socket.socket | None:
    url = cfg.get("url", "")
    port = int(cfg.get("port", 2101))
    mount = cfg.get("mount_point", "")
    password = cfg.get("password", "")

    parsed = urlparse(url if "://" in url else "http://" + url)
    host = parsed.hostname or url
    request = (
        f"SOURCE {password} /{mount} HTTP/1.0\r\n"
        f"User-Agent: NTRIP dock-of-the-base/1.0\r\n"
        f"Source-Agent: NTRIP dock-of-the-base/1.0\r\n"
        f"\r\n"
    )
    try:
        sock = socket.create_connection((host, port), timeout=10.0)
        sock.settimeout(10.0)
        sock.sendall(request.encode())
        buf = b""
        while len(buf) < 4096:
            chunk = sock.recv(256)
            if not chunk:
                break
            buf += chunk
            if b"\r\n" not in buf:
                continue
            if buf.startswith(b"ICY "):
                break
            if b"\r\n\r\n" in buf:
                break
        first_line = buf.split(b"\r\n")[0].decode(errors="ignore")
        if "200" not in first_line:
            log.warning("NTRIP-out: rejected — %s", first_line)
            sock.close()
            return None
        log.info("NTRIP-out: connected to %s/%s", host, mount)
        sock.settimeout(30.0)
        return sock
    except OSError as e:
        log.warning("NTRIP-out: connection failed — %s", e)
        return None


def _ntrip_out_recv(sock: socket.socket) -> bool:
    """Inner receive loop. Returns True to reconnect, False to stop."""
    global _ntrip_out_sock, _ntrip_out_active
    with _ntrip_out_lock:
        _ntrip_out_sock = sock
    try:
        while not _stop.is_set() and _ntrip_out_active:
            try:
                data = sock.recv(256)
                if not data:
                    log.warning("NTRIP-out: server closed connection")
                    return True
            except TimeoutError:
                continue
            except OSError as e:
                if _ntrip_out_active:
                    log.warning("NTRIP-out: socket error: %s", e)
                return True
        return False
    finally:
        with _ntrip_out_lock:
            _ntrip_out_sock = None
        try:
            sock.close()
        except OSError:
            pass


def _ntrip_out_loop(cfg: dict) -> None:
    """Outer reconnect loop with exponential backoff."""
    global _ntrip_out_active, _ntrip_out_status
    delay = NTRIP_OUT_RECONNECT_INITIAL
    while not _stop.is_set() and _ntrip_out_active:
        sock = _ntrip_out_connect(cfg)
        if sock:
            _ntrip_out_status = "connected"
            delay = NTRIP_OUT_RECONNECT_INITIAL
            reconnect = _ntrip_out_recv(sock)
            if not reconnect:
                break
            if _ntrip_out_active:
                _ntrip_out_status = "reconnecting"
                log.info("NTRIP-out: reconnecting in %.0fs", delay)
        else:
            log.warning("NTRIP-out: connection failed, retrying in %.0fs", delay)
        _stop.wait(delay)
        delay = min(delay * 2, NTRIP_OUT_RECONNECT_MAX)
    _ntrip_out_active = False
    _ntrip_out_status = "disconnected"
    log.info("NTRIP-out: disconnected")


def ntrip_out_connect() -> str | None:
    """Connect to external NTRIP caster. Returns error string on failure, None on success."""
    global _ntrip_out_active, _ntrip_out_status, _ntrip_out_thread
    cfg = (load_config() or {}).get("external_caster")
    if not cfg or not cfg.get("url", "").strip():
        return "External caster URL is not configured."
    sock = _ntrip_out_connect(cfg)
    if not sock:
        return "Failed to connect to external caster."
    _ntrip_out_active = True
    _ntrip_out_status = "connected"
    _ntrip_out_thread = threading.Thread(
        target=_ntrip_out_loop, args=(cfg,), daemon=True, name="ntrip-out"
    )
    _ntrip_out_thread.start()
    return None


def ntrip_out_disconnect() -> None:
    global _ntrip_out_active
    _ntrip_out_active = False
    with _ntrip_out_lock:
        sock = _ntrip_out_sock
    if sock:
        try:
            sock.close()
        except OSError:
            pass


# --- RAWX / SFRBX file logging ---


def _log_write(data: bytes) -> None:
    """Write raw bytes to the current log file (no-op when logging is inactive)."""
    with _log_lock:
        if _log_file is not None:
            try:
                _log_file.write(data)
            except OSError:
                pass


def _open_log_file() -> None:
    """Open a new timestamped log file. Caller must hold _log_lock."""
    global _log_file
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = LOG_DIR / f"rawx_{ts}.ubx"
    _log_file = open(path, "wb")
    log.info("RAWX log opened: %s", path.name)


def _cleanup_old_logs() -> None:
    """Delete log and RINEX files that have exceeded the configured retention period."""
    cfg = load_config() or {}
    retention_days = int(cfg.get("logging", {}).get("retention_days", 2))
    if retention_days <= 0:
        return
    cutoff = datetime.now().timestamp() - retention_days * 86400
    for pattern in ("rawx_*.ubx", "rawx_*.obs", "rawx_*.nav"):
        for f in LOG_DIR.glob(pattern):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    log.info("Removed expired log: %s", f.name)
            except OSError:
                pass


def _convert_to_rinex(ubx_path: Path) -> None:
    """Convert a completed UBX log file to RINEX 2.11 OBS+NAV using convbin."""
    obs_path = ubx_path.with_suffix(".obs")
    nav_path = ubx_path.with_suffix(".nav")
    try:
        result = subprocess.run(
            [
                "convbin",
                "-v",
                "2.11",
                "-o",
                str(obs_path),
                "-n",
                str(nav_path),
                str(ubx_path),
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            log.info("RINEX conversion complete: %s", ubx_path.name)
        else:
            log.warning(
                "convbin failed for %s: %s", ubx_path.name, result.stderr.strip()
            )
    except FileNotFoundError:
        log.warning("convbin not found — RINEX conversion skipped")
    except subprocess.TimeoutExpired:
        log.warning("convbin timed out for %s", ubx_path.name)
    except Exception as e:
        log.warning("RINEX conversion failed for %s: %s", ubx_path.name, e)


def _rotate_log() -> None:
    """Called by the rotation timer: close current file, open a new one, clean up."""
    global _log_file, _log_rotate_timer
    completed_path: Path | None = None
    with _log_lock:
        if _log_file is not None:
            completed_path = Path(_log_file.name)
            try:
                _log_file.close()
            except OSError:
                pass
            _log_file = None
        cfg = load_config() or {}
        if cfg.get("logging", {}).get("enabled", False):
            _open_log_file()
    _cleanup_old_logs()
    _schedule_rotation()
    if completed_path is not None and completed_path.exists():
        threading.Thread(
            target=_convert_to_rinex,
            args=(completed_path,),
            daemon=True,
            name="rinex-conv",
        ).start()


def _schedule_rotation() -> None:
    """Schedule the next rotation timer based on the configured file duration."""
    global _log_rotate_timer
    cfg = load_config() or {}
    log_cfg = cfg.get("logging", {})
    if not log_cfg.get("enabled", False):
        return
    duration_h = float(log_cfg.get("file_duration_h", 1))
    _log_rotate_timer = threading.Timer(duration_h * 3600, _rotate_log)
    _log_rotate_timer.daemon = True
    _log_rotate_timer.start()


def start_logging() -> None:
    """Start logging if enabled in config and not already active (idempotent)."""
    global _log_file
    cfg = load_config() or {}
    if not cfg.get("logging", {}).get("enabled", False):
        return
    with _log_lock:
        if _log_file is not None:
            return  # already running
        _open_log_file()
    _cleanup_old_logs()
    _schedule_rotation()


def stop_logging() -> None:
    """Stop logging and close the current log file."""
    global _log_file, _log_rotate_timer
    if _log_rotate_timer is not None:
        _log_rotate_timer.cancel()
        _log_rotate_timer = None
    with _log_lock:
        if _log_file is not None:
            try:
                _log_file.close()
            except OSError:
                pass
            _log_file = None


def configure_logging(
    enabled: bool, file_duration_h: float, retention_days: int, raw_interval_s: int = 30
) -> None:
    """Persist logging config and immediately apply it (restart or stop logging)."""
    cfg = load_config() or {}
    cfg["logging"] = {
        "enabled": enabled,
        "raw_interval_s": raw_interval_s,
        "file_duration_h": file_duration_h,
        "retention_days": retention_days,
    }
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    stop_logging()
    if enabled:
        start_logging()


def _ctime_from_name(name: str) -> str:
    m = re.match(r"rawx_(\d{8})_(\d{6})\.", name)
    if m:
        return f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]} {m.group(2)[:2]}:{m.group(2)[2:4]}:{m.group(2)[4:]}"
    return ""


def list_log_files() -> list[dict]:
    """Return metadata for all log and RINEX files, newest first."""
    if not LOG_DIR.exists():
        return []
    files = []
    for f in LOG_DIR.glob("rawx_*.*"):
        ext = f.suffix.lstrip(".")
        if ext not in ("ubx", "obs", "nav"):
            continue
        try:
            stat = f.stat()
            files.append(
                {
                    "name": f.name,
                    "ext": ext,
                    "size": stat.st_size,
                    "ctime": _ctime_from_name(f.name),
                }
            )
        except OSError:
            pass
    files.sort(key=lambda x: x["ctime"], reverse=True)
    return files


def logging_active() -> bool:
    """Return True if a log file is currently open."""
    with _log_lock:
        return _log_file is not None


def delete_log_file(filename: str) -> None:
    try:
        (LOG_DIR / filename).unlink()
    except OSError:
        pass


def clear_log_files() -> None:
    """Delete all log and RINEX files. If logging is active, restart into a fresh file."""
    was_active = logging_active()
    if was_active:
        stop_logging()
    if LOG_DIR.exists():
        for pattern in ("rawx_*.ubx", "rawx_*.obs", "rawx_*.nav"):
            for f in LOG_DIR.glob(pattern):
                try:
                    f.unlink()
                except OSError:
                    pass
    if was_active:
        start_logging()


def send_config(ubx: UBXMessage, timeout: float = _CONFIG_TIMEOUT) -> bool:
    """Send a CFG-VALSET to the active serial connection and wait for ACK."""
    global _ack_ok
    with _config_lock:
        if _serial is None or not _serial.is_open:
            return False
        _ack_ok = False
        _ack_event.clear()
        try:
            _serial.write(ubx.serialize())
        except OSError:
            return False
        if _ack_event.wait(timeout):
            return _ack_ok
        log.warning("No ACK for %s within %.1fs (live)", ubx.identity, timeout)
        return False


def configure_network(tcp: int, ntrip: int, mount: str) -> None:
    """Save network config and restart affected servers without touching the serial connection."""
    global _tcp_server, _ntrip_server
    cfg = load_config() or {}
    cfg["tcp_port"] = tcp
    local_caster = cfg.get("local_caster", {})
    local_caster["port"] = ntrip
    local_caster["mount_point"] = mount or "BASE"
    cfg["local_caster"] = local_caster
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))

    _stop_tcp_server()
    if tcp:
        _tcp_server = _start_tcp_server(tcp)

    _stop_ntrip_server()
    if ntrip:
        _ntrip_server = _start_ntrip_server(ntrip, mount or "BASE")

    log.info(
        "Network config updated — TCP: %s, NTRIP local: %s/%s",
        tcp or "disabled",
        ntrip or "disabled",
        mount or "BASE",
    )


# --- Lifecycle ---


def start() -> None:
    global _thread, _ntrip_in_thread, _tcp_server, _ntrip_server
    _stop.clear()
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config() or {}
    if "tty_exclude" not in cfg:
        cfg["tty_exclude"] = DEFAULT_TTY_EXCLUDE
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    tcp_port_cfg = cfg.get("tcp_port", 0)
    if tcp_port_cfg and not _tcp_server:
        _tcp_server = _start_tcp_server(int(tcp_port_cfg))
    local_caster = cfg.get("local_caster", {})
    ntrip_port_cfg = local_caster.get("port", 0)
    if ntrip_port_cfg and not _ntrip_server:
        mount = local_caster.get("mount_point") or "BASE"
        _ntrip_server = _start_ntrip_server(int(ntrip_port_cfg), mount)
    ec = cfg.get("external_caster", {})
    if ec.get("url", "").strip() and not _ntrip_out_active:
        ntrip_out_connect()
    start_logging()
    _thread = threading.Thread(target=_connection_loop, daemon=True, name="receiver")
    _thread.start()


def stop(timeout: float = 5.0) -> None:
    _stop.set()
    ntrip_in_disconnect()
    ntrip_out_disconnect()
    if _thread and _thread.is_alive():
        _thread.join(timeout=timeout)
    if _ntrip_in_thread and _ntrip_in_thread.is_alive():
        _ntrip_in_thread.join(timeout=timeout)
    if _ntrip_out_thread and _ntrip_out_thread.is_alive():
        _ntrip_out_thread.join(timeout=timeout)
    _stop_tcp_server()
    _stop_ntrip_server()
    stop_logging()
