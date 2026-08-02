"""Options straddle/strangle strategy engine — faithful re-implementation of the
AlgoTest "NIFTY BUYING 2025" strategy, confirmed against its settings page + the
2,162-row trade ledger.

Design: a STATEFUL, minute-driven engine. `on_minute(t, ce_prem, pe_prem)` is fed
one minute of premiums and returns a list of ACTIONS ("buy"/"sell" a leg). The
SAME engine drives both the backtest (feed historical minutes, record actions as
trades) and the live runner (feed live minutes, place the actions as real orders
on each client's Tradejini account). Pure Python, no I/O — so it is unit-testable
and identical in backtest and live.

Confirmed strategy rules (verified field-by-field vs the AlgoTest .algtst + docs):
  - From entry_time (09:35): pick the CE and PE whose premium is nearest target
    (~Rs.50). Each leg ARMS at its 09:35 reference premium.
  - Momentum entry: FIRST entry (BUY) when the leg's premium rises >= momentum_pct
    (10%) above its 09:35 arming reference. (LegMomentum PercentageUp 10)
  - Stop-loss: sl_pct (5%) below entry. (LegStopLoss Percentage 5)
  - Trailing stop {InstrumentMove 15, StopLossMove 5}: high-water trail. Once the
    premium is up >=15% from entry the stop trails 5% below the running PEAK and only
    rises (before +15% the initial -5% hard stop holds). Verified against the AlgoTest
    ground-truth ledger + real Kite tape (2026-07-10): winners exit ~5% below the peak
    (Jul-9 CE 82.15 peak -> 77 exit), NOT the ratchet-from-entry the AlgoTest docs imply.
  - Trail-to-breakeven (AllLegs): the moment ANY leg's stop-loss is HIT, every other
    open leg's stop is lifted to at least its own entry. (TrailSLtoBreakeven.AllLegs)
  - Re-entry (LikeOriginal / Re-Cost): after a stop-out, re-enter the SAME strike
    when the premium returns to the ORIGINAL entry price, up to max_reentry (20)/day.
  - Square off everything at exit_time (15:20). One position-cycle per day.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StraddleConfig:
    entry_hm: tuple = (9, 35)
    exit_hm: tuple = (15, 20)
    target_premium: float = 50.0     # "Closest Premium" strike selection
    momentum_pct: float = 0.10       # +10% from 09:35 ref to make the FIRST entry
    sl_pct: float = 0.05             # initial hard stop: 5% below entry
    initial_sl_pct: float | None = None  # override initial stop; None = use sl_pct
    trail_instrument_move: float = 0.15  # LegTrailSL InstrumentMove: ratchet every +15% gain
    trail_sl_move: float = 0.05          # LegTrailSL StopLossMove: raise stop +5% of entry per step
    max_reentry: int = 20
    trail_to_breakeven: bool = True  # AllLegs: on ANY leg's SL hit, lift other legs' stops to entry
    lots: int = 1
    legs: tuple = ("CE", "PE")       # both legs, Buy


@dataclass
class _Leg:
    name: str
    armed: bool = True
    ref: float = 0.0          # 09:35 arming reference (FIRST entry triggers at ref*(1+mom))
    orig_entry: float = 0.0   # ORIGINAL entry price (Re-Cost re-entries return to this)
    in_pos: bool = False
    entry: float = 0.0
    hwm: float = 0.0          # high-water premium since entry
    sl: float = 0.0           # current stop level
    entries: int = 0
    rearm_ready: bool = True   # Re-Cost gate: a re-entry is only allowed once the premium has
                               # come back DOWN to/below orig_entry since the last exit. Without
                               # this, a WINNING trail-exit (premium still above cost) re-fires
                               # immediately and cascades — the churn that wiped the Jul-9 winner.
    trades: list = field(default_factory=list)  # (entry_t, entry_p, exit_t, exit_p)
    _pending_entry_t: object = None


def _hm(t):
    return (t.hour, t.minute)


class StraddleEngine:
    """One trading day for one client. Feed it minutes in order; it emits actions."""

    def __init__(self, cfg: StraddleConfig):
        self.cfg = cfg
        self.legs = {n: _Leg(n) for n in cfg.legs}
        self._armed_refs_set = False
        self._done = False

    def on_minute(self, t, prems: dict) -> list:
        """prems: {"CE": premium, "PE": premium}. Returns list of action dicts:
        {"side":"buy"|"sell","leg":"CE"|"PE","premium":float,"reason":str}."""
        cfg, acts = self.cfg, []
        hm = _hm(t)
        if self._done or hm < cfg.entry_hm:
            return acts

        # arm both legs at the entry-time reference premium (first eligible minute)
        if not self._armed_refs_set:
            for n, lg in self.legs.items():
                lg.ref = prems.get(n, 0.0)
            self._armed_refs_set = True

        # square-off: at/after exit time, close everything and stop for the day
        if hm >= cfg.exit_hm:
            for n, lg in self.legs.items():
                if lg.in_pos:
                    acts.append(self._exit(lg, t, prems[n], "square_off"))
            self._done = True
            return acts

        # 1) update open legs: HIGH-WATER TRAILING STOP {InstrumentMove 15, StopLossMove 5}.
        #    Ground truth (AlgoTest ledger `9 july.csv`, cross-checked against the real Kite
        #    minute path 2026-07-10): winners exit ~StopLossMove% BELOW the running PEAK, not
        #    at the lagging ratchet-from-entry the AlgoTest docs describe.
        #      Jul-9 CE 24300: entry 59.30, peak 82.15 -> AlgoTest exit 77.00 (~HWM*0.95).
        #      Jul-8 PE 23900: entry 56.65, peak 83.95 -> AlgoTest exit 79.45 (~HWM*0.95).
        #    The OLD ratchet (sl = entry*(1-.05+.05*floor(gain/15%))) locked only +5% at that
        #    peak (SL 62.27) and gave the whole move back — it could never reach 77 (the minute
        #    HIGH was 82.15, so ticks can't lock a ratchet SL above ~62). So the ratchet is
        #    disproven by the tape. Correct semantics: the trail ARMS once the premium is up
        #    >= InstrumentMove% from entry (before that the initial -sl_pct hard stop holds, so
        #    winners get early room); once armed the stop trails StopLossMove% below the
        #    high-water mark and only ever rises.
        for n, lg in self.legs.items():
            if not lg.in_pos:
                continue
            lg.hwm = max(lg.hwm, prems[n])
            if lg.entry and lg.hwm >= lg.entry * (1 + cfg.trail_instrument_move):
                lg.sl = max(lg.sl, lg.hwm * (1 - cfg.trail_sl_move))

        # 2) execute stop-outs. TrailSLtoBreakeven.AllLegs: the moment a leg's SL is HIT,
        #    lift every OTHER open leg's stop to at least its own entry (protect the survivor).
        for n, lg in self.legs.items():
            if lg.in_pos and prems[n] <= lg.sl:
                acts.append(self._exit(lg, t, lg.sl if prems[n] < lg.sl else prems[n], "stop"))
                if cfg.trail_to_breakeven:
                    for m, olg in self.legs.items():
                        if m != n and olg.in_pos:
                            olg.sl = max(olg.sl, olg.entry)

        # 2b) Re-Cost re-arm: a flat leg becomes eligible to re-enter only AFTER the premium
        #     has returned down to (or below) its original entry. A winning trail-exit leaves
        #     the premium above cost; without this gate the leg re-buys on the very next tick
        #     and cascades (Jul-9 CE: the +17/sh winner was churned to -6/sh by 7 instant
        #     re-entries). AlgoTest re-enters "at cost" — i.e. only once price has come back.
        for n, lg in self.legs.items():
            if not lg.in_pos and lg.orig_entry > 0 and prems[n] <= lg.orig_entry:
                lg.rearm_ready = True

        # 3) entries. FIRST entry: +momentum_pct above the 09:35 ref. RE-entries (Re-Cost /
        #    LikeOriginal): re-enter the SAME strike when the premium returns to the ORIGINAL
        #    entry — but only after the leg has re-armed (reset back to cost) since its last exit.
        for n, lg in self.legs.items():
            if lg.in_pos or lg.entries >= cfg.max_reentry:
                continue
            if lg.entries >= 1 and not lg.rearm_ready:
                continue
            trigger = lg.ref * (1 + cfg.momentum_pct) if lg.entries == 0 else lg.orig_entry
            if trigger > 0 and prems[n] >= trigger:
                lg.in_pos = True
                lg.entry = prems[n]
                if lg.entries == 0:
                    lg.orig_entry = prems[n]          # lock original entry for Re-Cost re-entries
                lg.hwm = prems[n]
                init_sl = cfg.initial_sl_pct if cfg.initial_sl_pct is not None else cfg.sl_pct
                lg.sl = prems[n] * (1 - init_sl)
                lg.entries += 1
                lg.rearm_ready = False   # consume the re-arm; must reset back to cost before next
                lg._pending_entry_t = t
                acts.append({"side": "buy", "leg": n, "premium": prems[n],
                             "reason": "momentum" if lg.entries == 1 else "recost"})
        return acts

    def _exit(self, lg: _Leg, t, px, reason) -> dict:
        lg.trades.append((lg._pending_entry_t, lg.entry, t, px))
        lg.in_pos = False
        return {"side": "sell", "leg": lg.name, "premium": px, "reason": reason}

    def day_pnl(self, lot_qty: int) -> float:
        """Realized P&L for the day (buy legs: exit - entry) * qty."""
        pnl = 0.0
        for lg in self.legs.values():
            for (_, ep, _, xp) in lg.trades:
                pnl += (xp - ep) * lot_qty
        return pnl

    def trade_rows(self):
        for lg in self.legs.values():
            for (et, ep, xt, xp) in lg.trades:
                yield {"leg": lg.name, "entry_t": et, "entry": ep, "exit_t": xt, "exit": xp}


@dataclass
class _ReLeg:
    """One re-selecting sleeve (CE or PE). Sequential: seek a ~50 candidate -> +10% momentum
    enter -> high-water trail -> on exit, re-select the current ~50 strike and repeat."""
    name: str
    cand: float = 0.0         # candidate strike being watched (0 = none)
    ref: float = 0.0          # locked ~50 reference premium for the candidate
    in_pos: bool = False
    strike: float = 0.0
    entry: float = 0.0
    hwm: float = 0.0
    sl: float = 0.0
    entries: int = 0
    trades: list = field(default_factory=list)   # (strike, entry_t, entry_p, exit_t, exit_p)
    _pending_t: object = None


class ReselectStraddleEngine:
    """Strike-RE-SELECTING variant of the niftybuying straddle (reverse-engineered from the AlgoTest
    ledger Jul 6-10; validated by reselect_v2 harness — Jul-9 day P&L +Rs768 vs ledger +Rs764).
    Unlike StraddleEngine (fixed 09:35 legs), each leg follows the trend: it re-picks the closest-to
    -target-premium strike after every exit, so a running move is re-engaged on fresh strikes (the
    thing the fixed engine misses). Feed it the FULL strike band each tick via on_tick().

    Rule per leg: SELECT closest-to-`target_premium` strike -> lock ref=its premium (~50) ->
    ENTER when that strike's premium >= ref*(1+momentum_pct) (+10%) -> high-water trail (arm at
    +trail_instrument_move, then trail_sl_move below the peak; initial sl_pct hard stop) -> on exit
    re-SELECT. Candidate stays STICKY (a strike running to +10% isn't abandoned) unless it drifts
    DOWN to <0.85*ref (dead) — then re-pick. Config = the SAME StraddleConfig as the fixed engine."""

    def __init__(self, cfg: StraddleConfig):
        self.cfg = cfg
        self.legs = {n: _ReLeg(n) for n in cfg.legs}
        self._done = False

    def on_tick(self, t, bands: dict) -> list:
        """bands = {"CE": {strike: premium, ...}, "PE": {strike: premium, ...}} for the whole band.
        Returns action dicts: {"side","leg","strike","premium","reason"}."""
        cfg, acts = self.cfg, []
        hm = _hm(t)
        if self._done or hm < cfg.entry_hm:
            return acts
        if hm >= cfg.exit_hm:
            for n, lg in self.legs.items():
                if lg.in_pos:
                    px = bands.get(n, {}).get(lg.strike, lg.entry)
                    acts.append(self._exit(lg, t, px, "square_off"))
            self._done = True
            return acts

        for n, lg in self.legs.items():
            avail = bands.get(n, {})
            if not avail:
                continue
            s50 = min(avail, key=lambda s: abs(avail[s] - cfg.target_premium))

            # 1) manage an open position: high-water trail + stop-out
            if lg.in_pos:
                p = avail.get(lg.strike)
                if p is not None:
                    lg.hwm = max(lg.hwm, p)
                    if lg.entry and lg.hwm >= lg.entry * (1 + cfg.trail_instrument_move):
                        lg.sl = max(lg.sl, lg.hwm * (1 - cfg.trail_sl_move))
                    if p <= lg.sl:
                        acts.append(self._exit(lg, t, lg.sl if p < lg.sl else p, "stop"))

            # 2) seek + enter on +10% momentum of the locked ~50 candidate
            if not lg.in_pos and lg.entries < cfg.max_reentry:
                if lg.cand == 0.0 or lg.cand not in avail or (lg.ref and avail[lg.cand] < 0.85 * lg.ref):
                    lg.cand, lg.ref = s50, avail[s50]          # (re)lock the closest-50 candidate
                p = avail.get(lg.cand)
                if p is not None and lg.ref > 0 and p >= lg.ref * (1 + cfg.momentum_pct):
                    lg.in_pos = True
                    lg.strike, lg.entry, lg.hwm = lg.cand, p, p
                    init_sl = cfg.initial_sl_pct if cfg.initial_sl_pct is not None else cfg.sl_pct
                    lg.sl = p * (1 - init_sl)
                    lg.entries += 1
                    lg._pending_t = t
                    reason = "momentum" if lg.entries == 1 else "reselect"
                    lg.cand, lg.ref = 0.0, 0.0
                    acts.append({"side": "buy", "leg": n, "strike": lg.strike,
                                 "premium": p, "reason": reason})
        return acts

    def _exit(self, lg: _ReLeg, t, px, reason) -> dict:
        lg.trades.append((lg.strike, lg._pending_t, lg.entry, t, px))
        lg.in_pos = False
        lg.cand, lg.ref = 0.0, 0.0
        return {"side": "sell", "leg": lg.name, "strike": lg.strike, "premium": px, "reason": reason}

    def day_pnl(self, lot_qty: int) -> float:
        return sum((xp - ep) * lot_qty for lg in self.legs.values()
                   for (_, _, ep, _, xp) in lg.trades)
