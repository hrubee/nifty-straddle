"""Tradejini live market-data for the NIFTY straddle — replaces Kite as the premium source.

CubePlus WebSocket (NxtradStream, vendored in nxtradstream.py) + the data-account token. Provides:
  - discover_chain(underlying)      -> {'expiry','lot','CE':{strike:excToken},'PE':{strike:excToken}}
                                       (band around ATM; shape mirrors the old Kite discover_legs())
  - ChainFeed                       -> WS feed over a set of excTokens; .ltp(tokens) mirrors Kite ltp()
  - discover_50prem_legs / LivePremiumFeed -> convenience helpers (single ~Rs50 CE/PE + their live feed)
  - OrderFeed                       -> WS order events (orders/positions/trades); local order cache for instant fills

Ticks carry ltp + bidPrice + askPrice, so the shadow can log REAL spreads (the slippage number).
"""
import threading
import time
import datetime as dt
from typing import Callable

import tradejini as tj
from nxtradstream import NxtradStream

WS_HOST = "api.tradejini.com"


def _token():
    key, tok = tj.individual_data_token()
    return key, tok


def current_weekly_expiry(scrips, underlying="NIFTY"):
    today = dt.date.today().isoformat()
    exps = sorted({str(s.get("expiry"))[:10] for s in scrips
                   if s.get("symbol") == underlying and s.get("optType") in ("CE", "PE") and s.get("expiry")})
    fut = [e for e in exps if e >= today]
    return fut[0] if fut else (exps[0] if exps else None)


def _nifty_spot():
    import urllib.request
    import json
    r = json.load(urllib.request.urlopen(urllib.request.Request(
        "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?interval=5m&range=1d",
        headers={"User-Agent": "Mozilla/5.0"}), timeout=15))
    return [x for x in r["chart"]["result"][0]["indicators"]["quote"][0]["close"] if x][-1]


def _sample(key, tok, extokens, secs=7):
    latest = {}

    def on_tick(ws, d):
        if isinstance(d, dict) and d.get("token") is not None and d.get("ltp") is not None:
            latest[str(d["token"])] = d["ltp"]

    def on_conn(ws, ev):
        ws.subscribeL1(["%s_NFO" % t for t in extokens])

    s = NxtradStream(WS_HOST, stream_cb=on_tick, connect_cb=on_conn)
    s.connect("%s:%s" % (key, tok))
    time.sleep(secs)
    try:
        s.disconnect()
    except Exception:
        pass
    return latest


def discover_chain(underlying="NIFTY", band=2500, step=50):
    """Chain for the current weekly, restricted to a band around ATM (where the ~Rs50 legs live).
    Shape mirrors the old Kite discover_legs(): {'expiry','lot','CE':{strike:excToken},'PE':{...}}."""
    key, tok = _token()
    c = tj.TradejiniClient(tok, api_key=key)
    scrips = [s for s in c.list_scrips("NSEOptions") if s.get("symbol") == underlying]
    exp = current_weekly_expiry(scrips, underlying)
    wk = [s for s in scrips if str(s.get("expiry"))[:10] == exp]
    atm = round(_nifty_spot() / step) * step
    CE, PE, lot = {}, {}, 65
    for s in wk:
        try:
            k = float(s.get("strike") or 0)
        except (TypeError, ValueError):
            continue
        et = s.get("excToken")
        if not et:
            continue
        lot = int(s.get("lot") or lot)
        if s.get("optType") == "CE" and atm <= k <= atm + band:
            CE[k] = et
        elif s.get("optType") == "PE" and atm - band <= k <= atm:
            PE[k] = et
    return {"expiry": exp, "lot": lot, "CE": CE, "PE": PE}


class ChainFeed:
    """WS feed over a set of excTokens; maintains latest ltp (+ bid/ask). .ltp(tokens) returns
    {str(token): premium} — the exact shape the runner's Kite ltp() returned, so the runner is
    unchanged. Backs the runner's synchronous ltp() with the async CubePlus WS stream."""

    def __init__(self):
        self._latest = {}
        self._ba = {}
        self._lock = threading.Lock()
        self._s = None

    def _on_tick(self, ws, d):
        if isinstance(d, dict) and d.get("token") is not None:
            t = str(d["token"])
            with self._lock:
                if d.get("ltp") is not None:
                    self._latest[t] = float(d["ltp"])
                if d.get("bidPrice") is not None:
                    self._ba[t] = (d.get("bidPrice"), d.get("askPrice"))

    def start(self, extokens):
        key, tok = _token()
        ex = [str(t) for t in extokens]

        def on_conn(ws, ev):
            for i in range(0, len(ex), 500):
                ws.subscribeL1(["%s_NFO" % t for t in ex[i:i + 500]])

        self._s = NxtradStream(WS_HOST, stream_cb=self._on_tick, connect_cb=on_conn)
        self._s.connect("%s:%s" % (key, tok))
        return self

    def ltp(self, tokens):
        with self._lock:
            return {str(t): self._latest[str(t)] for t in tokens if str(t) in self._latest}

    def bidask(self, tokens):
        with self._lock:
            return {str(t): self._ba[str(t)] for t in tokens if str(t) in self._ba}

    def wait_ready(self, tokens, secs=8):
        end = time.time() + secs
        while time.time() < end:
            with self._lock:
                if any(str(t) in self._latest for t in tokens):
                    return True
            time.sleep(0.5)
        return False

    def stop(self):
        try:
            self._s.disconnect()
        except Exception:
            pass


def discover_50prem_legs(underlying="NIFTY", target=50.0, band=2500, step=50):
    """Convenience: the single ~Rs50 CE + PE for the current weekly (samples live premiums)."""
    key, tok = _token()
    c = tj.TradejiniClient(tok, api_key=key)
    scrips = [s for s in c.list_scrips("NSEOptions") if s.get("symbol") == underlying]
    exp = current_weekly_expiry(scrips, underlying)
    wk = [s for s in scrips if str(s.get("expiry"))[:10] == exp]
    spot = _nifty_spot()
    atm = round(spot / step) * step
    cand = {"CE": [], "PE": []}
    for s in wk:
        try:
            k = float(s.get("strike") or 0)
        except (TypeError, ValueError):
            continue
        cp = s.get("optType")
        if cp == "CE" and atm <= k <= atm + band:
            cand["CE"].append(s)
        elif cp == "PE" and atm - band <= k <= atm:
            cand["PE"].append(s)
    ex_all = [s.get("excToken") for s in cand["CE"] + cand["PE"] if s.get("excToken")]
    prem = _sample(key, tok, ex_all)
    out = {"expiry": exp, "spot": spot, "atm": atm}
    for cp in ("CE", "PE"):
        best = None
        for s in cand[cp]:
            p = prem.get(str(s.get("excToken")))
            if p is None or p <= 0:
                continue
            if best is None or abs(p - target) < abs(best[1] - target):
                best = (s, p)
        if best:
            s, p = best
            out[cp] = {"excToken": s.get("excToken"), "strike": float(s.get("strike")),
                       "disp": s.get("dispName"), "prem": p, "sym_id": s.get("id"),
                       "lot": int(s.get("lot") or 65)}
    return out


class LivePremiumFeed:
    """Continuous L1 stream for two chosen legs (by excToken). .premiums() -> {'CE','PE'}."""

    def __init__(self, ce_excToken, pe_excToken):
        self._map = {str(ce_excToken): "CE", str(pe_excToken): "PE"}
        self._prem = {"CE": None, "PE": None}
        self._ba = {"CE": (None, None), "PE": (None, None)}
        self._lock = threading.Lock()
        self._s = None

    def _on_tick(self, ws, d):
        if isinstance(d, dict) and d.get("token") is not None:
            leg = self._map.get(str(d["token"]))
            if not leg:
                return
            with self._lock:
                if d.get("ltp") is not None:
                    self._prem[leg] = d["ltp"]
                if d.get("bidPrice") is not None:
                    self._ba[leg] = (d.get("bidPrice"), d.get("askPrice"))

    def _on_conn(self, ws, ev):
        ws.subscribeL1(["%s_NFO" % t for t in self._map])

    def start(self):
        key, tok = _token()
        self._s = NxtradStream(WS_HOST, stream_cb=self._on_tick, connect_cb=self._on_conn)
        self._s.connect("%s:%s" % (key, tok))
        return self

    def premiums(self):
        with self._lock:
            return dict(self._prem)

    def bidask(self):
        with self._lock:
            return dict(self._ba)

    def stop(self):
        try:
            self._s.disconnect()
        except Exception:
            pass


# ── Phase 3: WebSocket Order Events for instant fill detection ─────
class OrderFeed:
    """WebSocket feed for Tradejini order/position/trade events.

    Subscribes to `orders`, `positions`, `trades` event types via the
    official SDK's `subscribeEvents()` method. Maintains a local order cache
    keyed by orderId so the runner can get instant fill confirmation instead
    of polling REST.

    Usage:
        feed = OrderFeed(api_key, access_token)
        feed.on_order_event(my_callback)
        feed.start()

        # Later, check if an order is filled:
        info = feed.get_order(order_id)
        if info and info["status"] in ("complete", "filled", "executed"):
            fill_qty = info["fill_qty"]
            avg_px = info["avg_px"]
    """

    def __init__(self, api_key: str, access_token: str):
        self._api_key = api_key
        self._access_token = access_token
        self._callbacks: list[Callable[[dict], None]] = []
        self._order_cache: dict[str, dict] = {}  # order_id -> {status, fill_qty, avg_px, side, symbol, ...}
        self._lock = threading.Lock()
        self._s: NxtradStream | None = None
        self._connected = False

    def on_order_event(self, cb: Callable[[dict], None]) -> None:
        """Register a callback for every order event received."""
        self._callbacks.append(cb)

    def _on_event(self, ws, d: dict) -> None:
        """Internal handler for order/position/trade events."""
        if not isinstance(d, dict):
            return

        evnt_type = d.get("evntType")
        if evnt_type not in ("orders", "positions", "trades"):
            return

        # For orders: update cache with latest status/fill
        if evnt_type == "orders":
            oid = str(d.get("orderId", ""))
            if oid:
                with self._lock:
                    self._order_cache[oid] = {
                        "status": d.get("status", "").lower(),
                        "fill_qty": int(float(d.get("filledQty", d.get("fillQty", 0)) or 0)),
                        "avg_px": float(d.get("price", 0) or 0),
                        "side": d.get("side", "").lower(),
                        "symbol": d.get("trdSym", d.get("symbol", "")),
                        "order_type": d.get("orderType", d.get("type", "")).lower(),
                        "reason": d.get("reason", ""),
                        "last_update": time.time(),
                    }

        # Fire callbacks (keep fast; exceptions in callbacks are isolated)
        for cb in self._callbacks:
            try:
                cb(d)
            except Exception:
                pass  # callback errors must not disrupt the feed

    def _on_conn(self, ws, ev: dict) -> None:
        """On connect: subscribe to order/position/trade events."""
        if ev.get("s") == "connected":
            self._connected = True
            # Subscribe to all three event types (no symbol list needed)
            ws.subscribeEvents(["orders", "positions", "trades"])

    def start(self) -> "OrderFeed":
        """Connect and start the WebSocket feed."""
        token = f"{self._api_key}:{self._access_token}"
        self._s = NxtradStream(WS_HOST, stream_cb=self._on_event, connect_cb=self._on_conn)
        self._s.connect(token)
        return self

    def get_order(self, order_id: str) -> dict | None:
        """Return cached order info (status, fill_qty, avg_px) or None if unknown."""
        with self._lock:
            return self._order_cache.get(str(order_id))

    def wait_for_fill(self, order_id: str, timeout_sec: float = 10.0) -> dict | None:
        """Block until order reaches a terminal state or timeout.
        Returns the cached order info or None if timeout."""
        end = time.time() + timeout_sec
        while time.time() < end:
            info = self.get_order(order_id)
            if info and info["status"] in ("complete", "completed", "filled", "executed",
                                            "cancelled", "canceled", "rejected"):
                return info
            time.sleep(0.2)
        return self.get_order(order_id)

    def is_connected(self) -> bool:
        return self._connected

    def stop(self) -> None:
        """Disconnect and clean up."""
        self._connected = False
        if self._s:
            try:
                self._s.disconnect()
            except Exception:
                pass
            self._s = None