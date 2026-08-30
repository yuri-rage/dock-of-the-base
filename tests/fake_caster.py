"""A minimal in-process NTRIP caster for exercising receiver.py's session lifecycle.

Speaks both halves of the protocol the app uses:

* ``SOURCE <pw> /<mount>``  -- what ``ntrip_out`` sends when uploading corrections
* ``GET /<mount>``          -- what ``ntrip_in`` sends when consuming them

It tracks how many sessions are open on each mount at any moment. That is the
property the lifecycle code has to preserve: exactly one. Counting sessions
server-side is the only reliable way to see a duplicate, because from inside the
app a leaked thread looks identical to a healthy one.

Note on overlap tolerance: a brief two-session blip is expected and harmless.
When the client tears down socket A and immediately opens socket B, this server
may not have processed A's FIN yet, so it momentarily sees both. That artifact
lasts well under a millisecond. A real leak lasts as long as the socket timeout
(30s before the fix). Tests therefore assert on *overlap duration*, not on a
peak of exactly 1 -- see OVERLAP_TOLERANCE_S.
"""

from __future__ import annotations

import socketserver
import threading
import time

# Generous enough to swallow FIN-processing lag, far below a real leak.
OVERLAP_TOLERANCE_S = 0.25

_RTCM_FRAME = b"\xd3\x00\x13" + b"\x00" * 20


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        caster: FakeCaster = self.server.caster  # type: ignore[attr-defined]
        kind = None
        try:
            self.request.settimeout(5.0)
            buf = b""
            while b"\r\n\r\n" not in buf and len(buf) < 4096:
                chunk = self.request.recv(256)
                if not chunk:
                    return
                buf += chunk

            if buf.startswith(b"SOURCE "):
                kind = "out"
            elif buf.startswith(b"GET "):
                kind = "in"
            else:
                return

            if caster._take_reject():
                self.request.sendall(b"ERROR - Bad Password\r\n")
                return

            self.request.sendall(b"ICY 200 OK\r\n\r\n")
            caster._open(kind)
            try:
                self._pump(kind)
            finally:
                caster._close(kind)
        except OSError:
            if kind is not None:
                pass

    def _pump(self, kind: str) -> None:
        """Hold the session open until the peer goes away."""
        while True:
            if kind == "in":
                # Feed the client corrections so its receive loop stays busy.
                try:
                    self.request.sendall(_RTCM_FRAME)
                except OSError:
                    return
            try:
                self.request.settimeout(0.01)
                if not self.request.recv(4096):
                    return  # clean FIN
            except TimeoutError:
                pass
            except OSError:
                return
            time.sleep(0.005)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class FakeCaster:
    """Counts concurrent NTRIP sessions per direction."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._live = {"in": 0, "out": 0}
        self._peak = {"in": 0, "out": 0}
        self._total = {"in": 0, "out": 0}
        self._overlap_since: dict[str, float | None] = {"in": None, "out": None}
        self._overlaps: dict[str, list[float]] = {"in": [], "out": []}
        self._reject_next = False

        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.caster = self  # type: ignore[attr-defined]
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    # --- session accounting -------------------------------------------------

    def _open(self, kind: str) -> None:
        with self._lock:
            self._live[kind] += 1
            self._total[kind] += 1
            self._peak[kind] = max(self._peak[kind], self._live[kind])
            if self._live[kind] > 1 and self._overlap_since[kind] is None:
                self._overlap_since[kind] = time.monotonic()

    def _close(self, kind: str) -> None:
        with self._lock:
            self._live[kind] -= 1
            started = self._overlap_since[kind]
            if self._live[kind] <= 1 and started is not None:
                self._overlaps[kind].append(time.monotonic() - started)
                self._overlap_since[kind] = None

    def _take_reject(self) -> bool:
        with self._lock:
            hit, self._reject_next = self._reject_next, False
            return hit

    # --- assertions surface -------------------------------------------------

    def live(self, kind: str) -> int:
        with self._lock:
            return self._live[kind]

    def peak(self, kind: str) -> int:
        with self._lock:
            return self._peak[kind]

    def total(self, kind: str) -> int:
        with self._lock:
            return self._total[kind]

    def max_overlap(self, kind: str) -> float:
        """Longest span with >1 concurrent session, including one in progress."""
        with self._lock:
            done = max(self._overlaps[kind], default=0.0)
            started = self._overlap_since[kind]
            ongoing = time.monotonic() - started if started is not None else 0.0
            return max(done, ongoing)

    def reject_next_handshake(self) -> None:
        """Force the next connection attempt to fail, to reach the backoff path."""
        with self._lock:
            self._reject_next = True

    def wait_for(self, kind: str, count: int, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.live(kind) == count:
                return True
            time.sleep(0.01)
        return False

    def config(self, mount: str = "TEST") -> dict[str, object]:
        return {"url": "127.0.0.1", "port": self.port, "mount_point": mount}

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()


__all__ = ["OVERLAP_TOLERANCE_S", "FakeCaster"]
