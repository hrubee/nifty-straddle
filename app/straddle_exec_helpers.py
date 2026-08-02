"""Pure helpers for the straddle limit-at-trigger executor fix (go-trader-3424).

Stdlib-only, no app imports -- so the fill-classification / limit-pricing /
engine-revert logic is unit-testable without the backend's DB/pydantic stack
(the same "extract pure helpers from subprocess wrappers" pattern the Go side
uses). straddle_executor.py and straddle_runner.py import from here.

Why this exists: _buy_one used to fire a MARKET buy at whatever the book
offered -- the engine's virtual entry (the +10% trigger price) and the real fill
could diverge by the whole spread, and a fast market could fill far above the
trigger. The fix places an IOC LIMIT at trigger*(1+buffer) instead: bounded
slippage by construction, and a confirmed NO-FILL is a clean SKIP (the runner
then reverts the engine leg to flat so the books never carry a position nobody
holds).
"""
from __future__ import annotations

# NSE option tick size (Rs). Limit prices must land on this grid or the venue
# rejects the order.
OPTION_TICK = 0.05

# _buy_one outcome classes -- chosen so the runner can decide the engine-desync
# revert without guessing:
#   BUY_OPEN    -- a position exists for this client (real fill, partial fill,
#                  dry-run, or the idempotent already-open guard).
#   BUY_NOFILL  -- CONFIRMED nothing at the venue (IOC came back zero-filled, or
#                  the broker synchronously rejected the order).
#   BUY_UNKNOWN -- an order MAY exist (network error mid-place, fill-status read
#                  failed). Never revert on this: reconcile/guardian own it.
#   BUY_NOORDER -- no order was even attempted (connection vanished, daily-loss
#                  cap). Deterministically no position, but not an IOC skip.
BUY_OPEN = "open"
BUY_NOFILL = "nofill"
BUY_UNKNOWN = "unknown"
BUY_NOORDER = "noorder"

# Order-book statuses that mean the order's life is over. An IOC is immediate-
# or-cancel at the venue, so one of these is the only honest end state; anything
# else after the poll window is classified UNKNOWN, never NOFILL.
_TERMINAL_STATUSES = {"complete", "completed", "filled", "executed",
                      "cancelled", "canceled", "rejected"}
_FILLED_STATUSES = ("complete", "completed", "filled", "executed")

# Tradejini OMS field names are not formally documented for the order book;
# probe the plausible spellings (same multi-key defensive pattern as
# TradejiniClient.day_realized_pnl_inr). First present+parsable key wins.
_FILL_QTY_KEYS = ("fillShares", "filledQty", "fillQty", "filled_qty",
                  "tradedQty", "fillshares", "executedQty")
_AVG_PX_KEYS = ("avgPrice", "avgPrc", "avgTradePrice", "avg_price",
                "fillPrice", "avgFillPrice")


def limit_buy_price(premium, buffer_pct=0.01, tick=OPTION_TICK):
    """The IOC buy-limit cap for an entry whose engine trigger is premium:
    premium*(1+buffer), rounded UP to the venue tick (rounding down could price
    the cap below the intended aggressiveness). Returns 0.0 for a non-positive
    premium -- the caller must treat that as un-priceable and place nothing."""
    if premium is None or premium <= 0 or tick <= 0:
        return 0.0
    raw = float(premium) * (1.0 + float(buffer_pct))
    ticks = int(raw / tick)
    if raw - ticks * tick > 1e-9:          # not already on the grid: next tick up
        ticks += 1
    return round(ticks * tick, 2)


def limit_sell_price(premium, buffer_pct=0.01, tick=OPTION_TICK):
    """The IOC sell-limit floor for an exit whose engine trigger is premium:
    premium*(1-buffer), rounded DOWN to the venue tick (rounding up could price
    the floor above the intended aggressiveness). Returns 0.0 for a non-positive
    premium -- the caller must treat that as un-priceable and place nothing."""
    if premium is None or premium <= 0 or tick <= 0:
        return 0.0
    raw = float(premium) * (1.0 - float(buffer_pct))
    ticks = int(raw / tick)
    # Round DOWN: we want the limit to be <= the trigger*(1-buffer),
    # so if not already on the grid, we stay at the lower tick
    return round(ticks * tick, 2)


def is_terminal_status(status):
    """True when an order-book status string is an end state for the order."""
    return str(status or "").strip().lower() in _TERMINAL_STATUSES


def extract_fill(order_row):
    """(status_lower, filled_qty, avg_price) from a raw Tradejini order-book row.
    Missing/garbage fields degrade to (status, 0, 0.0) -- but a terminal status
    with qty 0 reads as a confirmed no-fill, so a FILLED status whose qty fields
    are unreadable is reported with qty -1 ('filled, size unknown'): it can then
    never be misclassified NOFILL, and the caller falls back to the intended
    order qty."""
    row = order_row or {}
    status = str(row.get("status", "")).strip().lower()
    qty, qty_found = 0, False
    for k in _FILL_QTY_KEYS:
        if k in row:
            try:
                qty = int(float(row[k]))
                qty_found = True
                break
            except (TypeError, ValueError):
                continue
    avg = 0.0
    for k in _AVG_PX_KEYS:
        if k in row:
            try:
                avg = float(row[k])
                break
            except (TypeError, ValueError):
                continue
    if not qty_found and status in _FILLED_STATUSES:
        qty = -1   # filled for sure, size unreadable -- caller uses intended qty
    return status, qty, avg


def aggregate_buy_outcomes(outcomes):
    """Fold per-client _buy_one outcomes for ONE engine buy action into the
    runner-facing verdict. revert (put the engine leg back to flat) is True only
    when the miss is PROVEN everywhere: at least one client's IOC confirmed
    zero-fill, and no client has (or may have) a position. A single BUY_OPEN or
    BUY_UNKNOWN vetoes the revert -- the engine must keep managing a position
    that exists (or might)."""
    counts = {BUY_OPEN: 0, BUY_NOFILL: 0, BUY_UNKNOWN: 0, BUY_NOORDER: 0}
    for o in outcomes or []:
        counts[o if o in counts else BUY_UNKNOWN] += 1
    return {
        "clients": len(outcomes or []),
        "open": counts[BUY_OPEN],
        "nofill": counts[BUY_NOFILL],
        "unknown": counts[BUY_UNKNOWN],
        "noorder": counts[BUY_NOORDER],
        "revert": (counts[BUY_NOFILL] > 0 and counts[BUY_OPEN] == 0
                   and counts[BUY_UNKNOWN] == 0),
    }


def revert_faithful_leg(leg):
    """Put a FaithfulStraddleEngine leg (_FaLeg) back to FLAT after a confirmed
    all-clients no-fill, as if the entry never happened: position cleared, the
    consumed entry count refunded (max_reentry must not burn on a miss, and the
    momentum/reexecute reason derives from it), candidate cleared so the engine
    re-picks a fresh ~50 candidate on the next minute close and demands a fresh
    +10% trigger from THERE (re-arming at the stale trigger would book a
    fictitious cheap entry the market already left behind).

    Lives OUTSIDE options_straddle.py on purpose (task constraint): the engine
    stays a pure market-model; executor-reality repairs belong to the runner."""
    leg.in_pos = False
    leg.entries = max(0, int(leg.entries) - 1)
    leg.strike = 0.0
    leg.entry = 0.0
    leg.hwm = 0.0
    leg.sl = 0.0
    leg.cand = 0.0
    leg.ref = 0.0
    leg.rset_min = None
    leg._pending_t = None
