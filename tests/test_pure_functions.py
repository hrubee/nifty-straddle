"""Pure function tests for nifty-straddle standalone repo.

Tests the pure (non-DB) execution hardening fixes:
  - _in_late_start_refuse_window boundaries (Fix #3)
  - _carry quote gap (Fix #4)
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app")))

from app.straddle_runner import _in_late_start_refuse_window, _carry


def test_late_start_refuse_window_boundaries():
    f = _in_late_start_refuse_window
    # before the window → PROCEED
    for hm in ("00:01", "08:00", "09:14", "09:15", "09:30", "09:33", "09:35", "09:36"):
        assert f(hm) is False, f"{hm} should PROCEED (normal/early start)"
    # inside the window [09:37, 15:20] → REFUSE
    for hm in ("09:37", "09:38", "10:00", "12:04", "14:59", "15:19", "15:20"):
        assert f(hm) is True, f"{hm} should REFUSE (restart inside trading window)"
    # at/after the 15:20 square-off → PROCEED
    for hm in ("15:21", "15:25", "15:30", "16:00", "23:52", "23:59"):
        assert f(hm) is False, f"{hm} should PROCEED (post-square-off / off-hours)"
    print("ok: late-start refuse window boundaries (09:37–15:20 refuse, else proceed)")


def test_carry_quote_gap():
    c = _carry
    assert c(12.5, None) == 12.5
    assert c(12.5, 9.0) == 12.5
    assert c(None, 9.0) == 9.0
    assert c(None, None) is None
    assert c(0.0, 9.0) == 0.0
    print("ok: carry keeps fresh quote, carries on None, treats 0.0 as real")


if __name__ == "__main__":
    test_late_start_refuse_window_boundaries()
    test_carry_quote_gap()
    print("\nALL PURE FUNCTION TESTS PASSED")
