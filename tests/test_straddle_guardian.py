"""Guardian / restart-safety unit tests (post-2026-06-03 incident).

Pins the safety contract that lets an INDEPENDENT guardian force-close an orphaned
straddle position WITHOUT ever racing the live runner (which would double-sell a
long into a naked short). Pure-logic + file-I/O only — no broker, no DB:

  - `_guardian_action` truth table  — the close/skip decision for every state.
  - heartbeat write↔read round-trip  — the runner's liveness/ownership signal.
  - single-instance lock primitive   — two runners can't manage at once.
  - `managed_keys` (skipped if sqlalchemy absent) — per-(user,sym_id) ownership,
    so a client SKIPPED in sizing is correctly an orphan (the exact incident).

Run:  ../../.venv/bin/python tests/test_straddle_guardian.py
  or:  <backend-venv>/bin/python -m pytest tests/test_straddle_guardian.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.straddle_watchdog import _guardian_action, _read_heartbeat  # noqa: E402
from app import straddle_runner  # noqa: E402


# ── 1. the guardian decision truth table ────────────────────────
def test_guardian_action_truth_table():
    # not ours → never touch someone else's NIFTY position
    assert _guardian_action(is_ours=False, key="u|S", is_managed=False,
                            hb_stale=False, prev_orphans=set(), can_act=True) == "untracked"
    assert _guardian_action(is_ours=False, key="u|S", is_managed=True,
                            hb_stale=True, prev_orphans={"u|S"}, can_act=True) == "untracked"

    # ours + a LIVE runner managing it (fresh hb, in managed) → skip, never race
    assert _guardian_action(True, "u|S", is_managed=True,
                            hb_stale=False, prev_orphans=set(), can_act=True) == "skip"

    # DEAD runner (stale hb) + ours → close NOW, even first sight, even if "managed"
    # (a stale managed set belongs to a dead runner and must not be trusted)
    assert _guardian_action(True, "u|S", is_managed=True,
                            hb_stale=True, prev_orphans=set(), can_act=True) == "close"
    assert _guardian_action(True, "u|S", is_managed=False,
                            hb_stale=True, prev_orphans=set(), can_act=True) == "close"

    # FRESH runner but pair NOT managed (restart-skip OR in-flight close):
    #   first sight        → watch (wait one poll; an in-flight close settles)
    #   2nd consecutive    → close (a genuine orphan persists)
    assert _guardian_action(True, "u|S", is_managed=False,
                            hb_stale=False, prev_orphans=set(), can_act=True) == "watch"
    assert _guardian_action(True, "u|S", is_managed=False,
                            hb_stale=False, prev_orphans={"u|S"}, can_act=True) == "close"

    # outside the act window (time-partition vs the 15:25 square-off) → never close,
    # even a dead-runner orphan — the square-off backstop owns the EOD flatten
    assert _guardian_action(True, "u|S", is_managed=False,
                            hb_stale=True, prev_orphans={"u|S"}, can_act=False) == "defer"
    assert _guardian_action(True, "u|S", is_managed=False,
                            hb_stale=False, prev_orphans=set(), can_act=False) == "defer"
    print("ok: guardian_action truth table")


# ── 2. the per-(client) restart-skip scenario (advisor fix #3) ──
def test_restart_skip_is_an_orphan_for_skipped_client_only():
    """Restart picks the SAME strike; one client (Samir) is skipped in sizing
    because his cash is tied up. The runner manages it for the others but NOT for
    Samir. Guardian must close Samir's leftover and leave the managed ones."""
    sym = "S_PE_22800"
    managed = {f"radian|{sym}", f"other|{sym}"}   # runner manages it for these two
    # Samir's identical-strike position — NOT in managed → orphan, escalates to close
    assert _guardian_action(True, f"samir|{sym}", f"samir|{sym}" in managed,
                            hb_stale=False, prev_orphans={f"samir|{sym}"}, can_act=True) == "close"
    # Radian's same-strike position — IS managed → skip (no race)
    assert _guardian_action(True, f"radian|{sym}", f"radian|{sym}" in managed,
                            hb_stale=False, prev_orphans=set(), can_act=True) == "skip"
    print("ok: restart-skip orphan is per-(user,sym)")


# ── 3. heartbeat write (runner) ↔ read (guardian) round-trip ────
def test_heartbeat_round_trip(tmp_path=None):
    import tempfile
    d = tempfile.mkdtemp()
    hb = os.path.join(d, ".straddle_heartbeat.json")
    orig = straddle_runner._HB_PATH
    straddle_runner._HB_PATH = hb
    try:
        straddle_runner._write_heartbeat(["samir|S_PE"], {"PE"}, "live")
        from app import straddle_watchdog
        orig_w = straddle_watchdog.HEARTBEAT
        straddle_watchdog.HEARTBEAT = hb
        try:
            stale, managed, info = _read_heartbeat(stale_secs=180)
            assert stale is False, info
            assert managed == {"samir|S_PE"}, managed
            # a stale ts must read as stale (runner presumed dead)
            stale2, _, _ = _read_heartbeat(stale_secs=0)
            assert stale2 is True
            # clean exit removes the heartbeat → read as stale (missing)
            straddle_runner._clear_heartbeat()
            stale3, managed3, _ = _read_heartbeat(stale_secs=180)
            assert stale3 is True and managed3 == set()
        finally:
            straddle_watchdog.HEARTBEAT = orig_w
    finally:
        straddle_runner._HB_PATH = orig
    print("ok: heartbeat round-trip + staleness + clear")


# ── 4. single-instance lock primitive ───────────────────────────
def test_lock_is_exclusive_across_fds():
    """A second OS-level flock holder (≈ a 2nd process) must be refused while the
    runner holds the lock. We can't fork here, so assert the raw flock contract on
    the runner's lockfile: a fresh fd cannot also lock it."""
    import fcntl, tempfile
    d = tempfile.mkdtemp()
    lk = os.path.join(d, ".straddle_runner.lock")
    orig = straddle_runner._LOCK_PATH
    straddle_runner._LOCK_PATH = lk
    straddle_runner._lock_fp = None
    try:
        assert straddle_runner._acquire_lock() is True       # we take it
        assert straddle_runner._acquire_lock() is True       # idempotent in-process
        # a separate fd (stand-in for a 2nd process) must fail to lock it
        fp2 = open(lk, "w")
        try:
            try:
                fcntl.flock(fp2.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                raised = False
            except OSError:
                raised = True
            assert raised, "second flock holder should have been refused"
        finally:
            fp2.close()
    finally:
        if straddle_runner._lock_fp is not None:
            straddle_runner._lock_fp.close()
        straddle_runner._lock_fp = None
        straddle_runner._LOCK_PATH = orig
    print("ok: single-instance lock is exclusive")


# ── 5. managed_keys (needs sqlalchemy → skipped if absent) ──────
def test_managed_keys_per_user_sym():
    try:
        from app.straddle_executor import StraddleClientExecutor
    except Exception as e:
        print(f"skip: managed_keys (import unavailable: {str(e)[:60]})")
        return
    ex = StraddleClientExecutor.__new__(StraddleClientExecutor)
    ex._symcache = {("CE", 23800.0): {"sym_id": "SCE"},
                    ("PE", 22800.0): {"sym_id": "SPE"}}
    ex.sized = [{"user_id": "samir", "lots": 1}, {"user_id": "radian", "lots": 2}]
    assert set(ex.managed_keys({"PE"})) == {"samir|SPE", "radian|SPE"}
    assert set(ex.managed_keys({"CE", "PE"})) == {
        "samir|SCE", "radian|SCE", "samir|SPE", "radian|SPE"}
    assert ex.managed_keys(set()) == []          # flat → manages nothing
    # a SKIPPED client (not in sized) never appears → its leftover is an orphan
    ex.sized = [{"user_id": "radian", "lots": 2}]
    assert set(ex.managed_keys({"PE"})) == {"radian|SPE"}
    assert "samir|SPE" not in set(ex.managed_keys({"CE", "PE"}))
    print("ok: managed_keys per-(user,sym_id), skipped client excluded")


if __name__ == "__main__":
    test_guardian_action_truth_table()
    test_restart_skip_is_an_orphan_for_skipped_client_only()
    test_heartbeat_round_trip()
    test_lock_is_exclusive_across_fds()
    test_managed_keys_per_user_sym()
    print("\nALL GUARDIAN TESTS PASSED")
