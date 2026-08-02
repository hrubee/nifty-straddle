"""Pins the StraddleEngine to the verified niftybuying2025.algtst behaviour, validated against
the AlgoTest ground-truth ledger (`9 july.csv`) cross-checked with the real Kite tape (2026-07-10):
  1. LegTrailSL {InstrumentMove 15, StopLossMove 5}  — HIGH-WATER trail: arms after +15%, then
     rides 5% BELOW the running peak. (The earlier ratchet-from-entry read was disproven by the
     tape: Jul-9 CE 24300 peaked 82.15 and AlgoTest exited 77.00 (~HWM*0.95); the ratchet could
     only lock ~62 there and gave the whole move back. Do NOT revert to the ratchet.)
  2. LikeOriginal / Re-Cost re-entry                  — same strike, back to the ORIGINAL entry price
  3. TrailSLtoBreakeven.AllLegs                        — fires when ANY leg's SL is HIT
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import datetime as dt

from options_straddle import StraddleConfig, StraddleEngine, ReselectStraddleEngine


def _t(h, m):
    return dt.datetime(2026, 1, 1, h, m)


def _fresh():
    e = StraddleEngine(StraddleConfig())
    e.on_minute(_t(9, 35), {"CE": 50.0, "PE": 40.0})   # arm CE ref=50 (trigger 55), PE out of the way
    e.on_minute(_t(9, 36), {"CE": 56.0, "PE": 40.0})   # 56 > 55 -> BUY CE @56, initial SL 53.2
    return e, e.legs["CE"]


def test_trailing_stop_arms_at_15pct_then_rides_5pct_below_the_peak():
    e, ce = _fresh()
    assert ce.entry == 56.0 and abs(ce.sl - 53.2) < 1e-6      # initial -5%
    e.on_minute(_t(9, 40), {"CE": 62.0, "PE": 40.0})          # +10.7% (< +15%) -> not armed yet
    assert abs(ce.sl - 53.2) < 1e-6, "below +15% the stop must stay at the initial -5% (early room)"
    e.on_minute(_t(9, 41), {"CE": 65.0, "PE": 40.0})          # +16% -> ARMED -> 5% below peak 65
    assert abs(ce.sl - 61.75) < 1e-6, "once armed the stop rides 5% below the running peak"
    e.on_minute(_t(9, 42), {"CE": 73.0, "PE": 40.0})          # new peak 73 -> 5% below
    assert abs(ce.sl - 69.35) < 1e-6                          # 73 * 0.95
    e.on_minute(_t(9, 43), {"CE": 85.0, "PE": 40.0})          # new peak 85 -> 5% below
    assert abs(ce.sl - 80.75) < 1e-6                          # 85 * 0.95
    acts = e.on_minute(_t(9, 44), {"CE": 70.0, "PE": 40.0})   # pull back through 80.75 -> capture near peak
    stops = [a for a in acts if a["side"] == "sell" and a["reason"] == "stop"]
    assert stops and abs(stops[0]["premium"] - 80.75) < 1e-6, \
        "the winner must be booked ~5% below the peak (85 -> 80.75), not given back to the ratchet's ~62"


def test_captures_winner_near_peak_matching_algotest_ledger():
    # Real tape 2026-07-10 (Kite minute candles) for the two ledger winners AlgoTest booked.
    # Feeding the peak then the pullback, the engine must exit ~5% below the peak (HWM*0.95),
    # reproducing the AlgoTest fills within tick-slippage — NOT the old ratchet's giveback.
    for entry, peak, pullback, algotest_exit in [
        (59.30, 82.15, 60.80, 77.00),   # Jul-9 CE 24300: peak 82.15 -> AlgoTest 77.00
        (56.65, 83.95, 71.20, 79.45),   # Jul-8 PE 23900: peak 83.95 -> AlgoTest 79.45
    ]:
        e = StraddleEngine(StraddleConfig())
        ref = entry / 1.10                                    # so `entry` triggers the +10% momentum buy
        e.on_minute(_t(9, 35), {"CE": ref, "PE": 5.0})
        e.on_minute(_t(9, 36), {"CE": entry, "PE": 5.0})
        ce = e.legs["CE"]
        assert ce.in_pos and abs(ce.entry - entry) < 1e-6
        e.on_minute(_t(9, 37), {"CE": peak, "PE": 5.0})       # spike to the day's peak -> arm + trail
        expected_sl = peak * 0.95
        assert abs(ce.sl - expected_sl) < 1e-6
        acts = e.on_minute(_t(9, 38), {"CE": pullback, "PE": 5.0})  # pull back through the trailed stop
        sells = [a for a in acts if a["side"] == "sell" and a["reason"] == "stop"]
        assert sells, "pullback below the high-water stop must fire the trailing stop"
        assert abs(sells[0]["premium"] - expected_sl) < 1e-6
        # exit lands within slippage of what AlgoTest actually booked (was ~62 under the old ratchet)
        assert abs(sells[0]["premium"] - algotest_exit) < 1.5


def test_winning_trail_exit_does_not_cascade_reentries():
    # Regression for the Jul-9 CE 24300 churn: a WINNING trail-exit leaves the premium above
    # cost; the leg must NOT re-buy until the premium has come back DOWN to cost (Re-Cost re-arm).
    e = StraddleEngine(StraddleConfig())
    e.on_minute(_t(9, 35), {"CE": 51.0, "PE": 5.0})           # ref 51 -> +10% trigger 56.1
    e.on_minute(_t(9, 36), {"CE": 60.0, "PE": 5.0})           # momentum buy @60, orig_entry 60
    ce = e.legs["CE"]
    assert ce.in_pos and ce.orig_entry == 60.0
    e.on_minute(_t(9, 37), {"CE": 82.0, "PE": 5.0})           # spike -> arm trail, SL 82*.95=77.9
    acts = e.on_minute(_t(9, 38), {"CE": 77.0, "PE": 5.0})    # pull back below 77.9 -> trail-exit (WIN)
    assert any(a["side"] == "sell" and a["reason"] == "stop" for a in acts)
    assert not ce.in_pos and ce.entries == 1
    # premium still ABOVE cost (60) for several ticks -> must NOT re-enter (the old cascade bug)
    for px in (76.0, 72.0, 68.0, 63.0, 61.0):
        assert e.on_minute(_t(9, 39), {"CE": px, "PE": 5.0}) == [], "no re-entry while above cost"
    assert ce.entries == 1
    e.on_minute(_t(9, 40), {"CE": 58.0, "PE": 5.0})           # finally resets DOWN through cost -> re-arm
    acts2 = e.on_minute(_t(9, 41), {"CE": 60.0, "PE": 5.0})   # back up to cost -> ONE re-entry
    assert any(a["reason"] == "recost" for a in acts2) and ce.entries == 2


def test_reentry_is_recost_returns_to_original_entry():
    e, ce = _fresh()
    orig = ce.orig_entry
    assert orig == 56.0
    acts = e.on_minute(_t(9, 37), {"CE": 52.0, "PE": 40.0})   # 52 < 53.2 -> STOP
    assert any(a["side"] == "sell" and a["reason"] == "stop" for a in acts)
    assert not ce.in_pos and ce.entries == 1
    # premium drifts but does NOT reach a fresh +10% (61.6); Re-Cost re-enters at the ORIGINAL 56
    assert e.on_minute(_t(9, 40), {"CE": 55.0, "PE": 40.0}) == []   # 55 < 56 -> no re-entry yet
    acts2 = e.on_minute(_t(9, 45), {"CE": 56.0, "PE": 40.0})        # back to original entry -> re-enter
    buys = [a for a in acts2 if a["side"] == "buy"]
    assert buys and buys[0]["reason"] == "recost" and buys[0]["premium"] == 56.0
    assert ce.in_pos and ce.entries == 2


def test_breakeven_all_legs_fires_on_a_leg_stop_hit():
    e = StraddleEngine(StraddleConfig())
    e.on_minute(_t(9, 35), {"CE": 50.0, "PE": 50.0})
    e.on_minute(_t(9, 36), {"CE": 56.0, "PE": 56.0})          # both BUY @56, SL 53.2 each
    assert e.legs["CE"].in_pos and e.legs["PE"].in_pos
    assert abs(e.legs["CE"].sl - 53.2) < 1e-6
    e.on_minute(_t(9, 38), {"CE": 58.0, "PE": 52.0})          # PE 52<53.2 -> STOP -> CE lifted to entry
    assert abs(e.legs["CE"].sl - 56.0) < 1e-6, "a leg's SL hit must lift the surviving leg to breakeven"


def test_reselect_engine_follows_the_trend_to_a_new_strike():
    # The whole point of re-selection: after a leg exits, it re-picks the CURRENT ~50 strike, so a
    # continuing trend is re-engaged on a DIFFERENT strike (what the fixed engine cannot do).
    e = ReselectStraddleEngine(StraddleConfig())
    ce = lambda d: {"CE": d, "PE": {23000: 5.0}}   # PE parked out of the way
    e.on_tick(_t(9, 36), ce({24000: 80, 24100: 50, 24200: 30}))    # closest-50 = 24100, ref 50, no +10%
    assert e.legs["CE"].cand == 24100
    a1 = e.on_tick(_t(9, 37), ce({24000: 85, 24100: 56, 24200: 33}))   # 24100 hits 56 >= 55 -> ENTER
    assert any(x["side"] == "buy" and x["strike"] == 24100 and x["reason"] == "momentum" for x in a1)
    e.on_tick(_t(9, 38), ce({24100: 72, 24200: 40}))               # spike -> arm trail (sl 72*.95=68.4)
    a2 = e.on_tick(_t(9, 39), ce({24100: 67, 24200: 45}))          # pull back -> trail-exit ~68.4 (WIN)
    assert any(x["side"] == "sell" and x["reason"] == "stop" for x in a2)
    assert not e.legs["CE"].in_pos and e.legs["CE"].entries == 1
    # trend moved up: after the exit it re-locks the current ~50 strike (24200), then enters it on
    # its own +10% momentum -> re-engaged the trend on a DIFFERENT strike than the first entry.
    e.on_tick(_t(9, 40), ce({24100: 67, 24200: 46}))               # ~50 strike now 24200 -> lock ref 46
    a3 = e.on_tick(_t(9, 41), ce({24100: 67, 24200: 52, 24300: 30}))   # 24200 +13% -> re-enter, NEW strike
    buys = [x for x in a3 if x["side"] == "buy"]
    assert buys and buys[0]["strike"] == 24200 and buys[0]["reason"] == "reselect"
    assert e.legs["CE"].entries == 2 and e.legs["CE"].strike == 24200
    assert e.legs["CE"].strike != 24100          # the key win: re-engaged on a different strike


def test_reselect_engine_no_entry_until_momentum_and_squares_off():
    e = ReselectStraddleEngine(StraddleConfig())
    ce = lambda d: {"CE": d, "PE": {23000: 5.0}}
    assert e.on_tick(_t(9, 36), ce({24100: 50, 24200: 30})) == []   # ~50 selected but < +10% -> no entry
    e.on_tick(_t(9, 40), ce({24100: 60, 24200: 40}))                # +20% -> enter 24100
    assert e.legs["CE"].in_pos
    acts = e.on_tick(_t(15, 20), ce({24100: 90, 24200: 60}))        # exit time -> square off open leg
    assert any(x["reason"] == "square_off" for x in acts) and e._done


def test_breakeven_not_triggered_merely_by_profit():
    # the OLD bug lifted breakeven when a leg was +5% in profit; the spec requires a STOP hit.
    e = StraddleEngine(StraddleConfig())
    e.on_minute(_t(9, 35), {"CE": 50.0, "PE": 50.0})
    e.on_minute(_t(9, 36), {"CE": 56.0, "PE": 56.0})          # both in @56, SL 53.2
    e.on_minute(_t(9, 40), {"CE": 60.0, "PE": 54.0})          # CE +7% profit, PE alive (no stop)
    assert abs(e.legs["PE"].sl - 53.2) < 1e-6, "profit alone must NOT lift the other leg to breakeven"


if __name__ == "__main__":
    test_trailing_stop_arms_at_15pct_then_rides_5pct_below_the_peak()
    test_captures_winner_near_peak_matching_algotest_ledger()
    test_winning_trail_exit_does_not_cascade_reentries()
    test_reentry_is_recost_returns_to_original_entry()
    test_breakeven_all_legs_fires_on_a_leg_stop_hit()
    test_reselect_engine_follows_the_trend_to_a_new_strike()
    test_reselect_engine_no_entry_until_momentum_and_squares_off()
    test_breakeven_not_triggered_merely_by_profit()
    print("\nALL ENGINE SPEC TESTS PASSED")