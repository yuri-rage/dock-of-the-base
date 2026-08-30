# NTRIP lifecycle tests

Regression tests for the session-management code in `app/receiver.py`.

## Running

```bash
pip install pytest
pytest tests/ -q
```

Takes about 35 seconds. No network access needed — the tests stand up an
in-process NTRIP caster on a loopback port.

## Layout

| File | Purpose |
| --- | --- |
| `fake_caster.py` | In-process caster speaking both `SOURCE` (ntrip-out) and `GET` (ntrip-in). Counts concurrent sessions per mount. |
| `conftest.py` | Fixtures. Points `receiver` at a throwaway config, shortens reconnect intervals, and tears down worker threads between tests. |
| `test_ntrip_lifecycle.py` | The tests. |

## The invariant

At most one session per mount point, at any moment, in either direction.

Counting server-side is the only reliable way to see a violation: from inside
the app a leaked worker thread looks identical to a healthy one.

## Why duration, not peak

A momentary two-session reading is expected and harmless. When the client tears
down socket A and immediately opens socket B, the caster may not have processed
A's FIN yet and briefly sees both. That artifact lasts well under a millisecond.

A real leak lasts as long as the socket timeout — 30 seconds before the fix.
So the tests assert on *overlap duration* against `OVERLAP_TOLERANCE_S` (250ms)
rather than requiring a peak of exactly 1. An earlier version asserted on peak
and produced a false positive that took real effort to rule out.

## Tuning

`conftest.py` shrinks the reconnect intervals so a full backoff cycle fits in a
test. Production values are 30s (`NTRIP_IN_RECONNECT_INTERVAL`) and 5–60s
(`NTRIP_OUT_RECONNECT_*`), which is also the real width of the window in which
the resurrection bug was reachable.

## Coverage

Each test maps to a bug found by running this code rather than reading it:

1. `ntrip_out_connect` opened a socket, discarded it, and let the worker open a
   second — an orphaned upload on every connect.
2. Teardown called `close()` on a socket another thread was blocked in `recv()`
   on. On Linux the blocked call holds a reference to the file description, so
   no FIN was sent and the session survived until the 30s timeout.
   `shutdown(SHUT_RDWR)` first is what fixes it.
3. A worker parked in reconnect backoff would wake, see that a new connect had
   set the shared active flag back to `True`, and resurrect itself as a second
   session. Hit the output path first, then the input path.
4. Connection state mutated from several threads without a lock.

Two static checks guard the source directly: `test_no_nested_lock_acquisition`
walks the AST to confirm none of the seven locks is ever taken while another is
held (so no ordering cycle can form), and
`test_disconnect_shuts_down_before_closing` fails if the `shutdown()` call is
dropped in a refactor.

## Validation

Against the fixed branch: 15 passed, repeatable across runs.
Against the pre-fix `master`: 9 failed, 6 passed — each failure naming its bug,
e.g. `connect opened more than one socket -- the authenticated socket is being
discarded and reopened by the worker`.

A test that has never failed hasn't been shown to work. Check any new test here
against a deliberately broken build before trusting it.
