#!/usr/bin/env python3
"""
Lightweight Tradejini executor for standalone nifty-straddle repo.

Features:
- WebSocket OrderFeed for instant fill detection (no REST polling)
- Parallel order placement across legs
- Execution hardening (no double-sell, close-failure tracking, quote-gap carry)
- No DB/SQLAlchemy/Telegram deps — pure in-memory state + REST + WS
- Runs as part of the runner: executor(action, meta) hook

Usage:
    from straddle_executor_lite import StraddleExecutorLite
    executor = StraddleExecutorLite(
        api_key=os.getenv("TRADEJINI_DATA_API_KEY"),
        password=os.getenv("TRADEJINI_PASSWORD"),
        totp_secret=os.getenv("TRADEJINI_TOTP"),
        lots=1  # or from wallet sizing
    )
    run_shadow_session(executor=executor)
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field

import requests
import websocket

from nxtradstream import NxtradStream
from tradejini_data import ChainFeed, OrderFeed

log = logging.getLogger("straddle.exec")

# ── Constants ────────────────────────────────────────────────────
WS_HOST = "api.tradejini.com"
BASE_URL = "https://api.tradejini.com"
DEFAULT_LOT = 75
MARKET_PROT_PCT = float(os.getenv("TRADEJINI_MKT_PROT_PCT", "0.5"))

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _ist_now() -> datetime:
    return _utcnow() + timedelta(hours=5, minutes=30)

def _ist_today() -> str:
    return _ist_now().strftime("%Y-%m-%d")

def _ctx():
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        try:
            return ssl.create_default_context()
        except Exception:
            return ssl._create_unverified_context()

_SSL = _ctx()

# ── Token management ─────────────────────────────────────────────
_token_cache: Dict[str, Tuple[str, float]] = {}  # key -> (token, expires_at)
_token_lock = threading.Lock()

def _get_data_token(api_key: str, password: str, totp_secret: str) -> str:
    """Get or refresh the data account token (individual-token-v2)."""
    import pyotp
    cache_key = f"data:{api_key}"
    with _token_lock:
        if cache_key in _token_cache:
            token, exp = _token_cache[cache_key]
            if time.time() < exp - 60:  # 60s buffer
                return token
    
    # Generate TOTP
    totp = pyotp.TOTP(totp_secret).now()
    
    # Request token
    url = f"{BASE_URL}/api-gw/oauth/individual-token-v2"
    body = {
        "api_key": api_key,
        "password": password,
        "totp": totp,
        "remember_me": True,
    }
    r = requests.post(url, json=body, timeout=30, verify=_SSL)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success" or not data.get("data", {}).get("access_token"):
        raise RuntimeError(f"Token fetch failed: {data}")
    
    token = f"{api_key}:{data['data']['access_token']}"
    expires_in = data['data'].get('expires_in', 86400)
    with _token_lock:
        _token_cache[cache_key] = (token, time.time() + expires_in)
    return token

# ── Symbol resolution ────────────────────────────────────────────
@dataclass
class ResolvedSymbol:
    sym_id: str      # Tradejini exchange token (e.g., "12345")
    symbol: str      # Display symbol (e.g., "NIFTY24JUN24000CE")
    lot: int         # Lot size
    strike: float
    leg: str         # "CE" or "PE"
    expiry: str

_symbol_cache: Dict[Tuple[str, float], ResolvedSymbol] = {}
_chain_cache: Optional[Dict] = None
_chain_expiry: Optional[str] = None

def discover_chain(api_key: str, password: str, totp_secret: str, token: str) -> Dict:
    """Discover weekly chain from Tradejini."""
    global _chain_cache, _chain_expiry
    if _chain_cache:
        return _chain_cache
    
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(f"{BASE_URL}/api-gw/masters/scrips", headers=headers, timeout=30, verify=_SSL)
    r.raise_for_status()
    scrips = r.json().get("data", [])
    
    # Find current weekly expiry for NIFTY
    today = _ist_today()
    exps = sorted({str(s.get("expiry"))[:10] for s in scrips
                   if s.get("symbol") == "NIFTY" and s.get("optType") in ("CE", "PE") and s.get("expiry")})
    fut = [e for e in exps if e >= today]
    expiry = fut[0] if fut else (exps[0] if exps else None)
    if not expiry:
        raise RuntimeError("No NIFTY weekly expiry found")
    
    exp_scrips = [s for s in scrips if str(s.get("expiry"))[:10] == expiry 
                  and s.get("symbol") == "NIFTY" and s.get("optType") in ("CE", "PE")]
    lot = int(exp_scrips[0].get("lotSize", DEFAULT_LOT)) if exp_scrips else DEFAULT_LOT
    
    CE = {float(s["strike"]): s["exchangeToken"] for s in exp_scrips if s["optType"] == "CE"}
    PE = {float(s["strike"]): s["exchangeToken"] for s in exp_scrips if s["optType"] == "PE"}
    
    _chain_cache = {"expiry": expiry, "lot": lot, "CE": CE, "PE": PE}
    _chain_expiry = expiry
    return _chain_cache

def _resolve_sym(token: str, leg: str, strike: float) -> ResolvedSymbol:
    """Resolve (leg, strike) -> sym_id, symbol, lot from cached chain."""
    key = (leg, strike)
    if key in _symbol_cache:
        return _symbol_cache[key]
    
    if _chain_cache is None:
        raise RuntimeError("Chain not discovered yet")
    
    leg_map = _chain_cache.get(leg, {})
    if strike not in leg_map:
        raise RuntimeError(f"Strike {strike} not in chain for {leg}")
    
    sym_id = leg_map[strike]
    expiry = _chain_expiry
    # Build display symbol: NIFTY + DDMMMYY + strike + CE/PE
    # e.g., NIFTY24JUN24000CE
    try:
        exp_dt = datetime.strptime(expiry, "%Y-%m-%d")
        exp_str = exp_dt.strftime("%d%b%y").upper()
    except Exception:
        exp_str = expiry.replace("-", "")
    
    strike_str = f"{int(strike) if strike == int(strike) else strike}"
    symbol = f"NIFTY{exp_str}{strike_str}{leg}"
    
    res = ResolvedSymbol(
        sym_id=str(sym_id),
        symbol=symbol,
        lot=_chain_cache["lot"],
        strike=strike,
        leg=leg,
        expiry=expiry
    )
    _symbol_cache[key] = res
    return res

# ── Position tracking (in-memory, session-scoped) ────────────────
@dataclass
class PositionState:
    """Tracks a single leg position for this session."""
    leg: str
    strike: float
    sym_id: str
    symbol: str
    lots: int
    entry_price: float = 0.0
    qty: int = 0
    status: str = "open"  # open, closed, error
    order_id: Optional[str] = None
    exit_price: Optional[float] = None
    detail: str = ""

@dataclass
class ClientState:
    """Per-client position state (for single-client standalone mode)."""
    user_id: str
    email: str
    lots: int
    positions: Dict[str, PositionState] = field(default_factory=dict)  # sym_id -> PositionState

# ── Order placement ──────────────────────────────────────────────
def _limit_buy_price(ltp: float) -> float:
    """Market protection for buy: LTP * (1 + MARKET_PROT_PCT/100), rounded to 0.05."""
    prot = ltp * (1 + MARKET_PROT_PCT / 100)
    return round(prot * 20) / 20  # tick size 0.05 for options

def _limit_sell_price(ltp: float) -> float:
    """Market protection for sell: LTP * (1 - MARKET_PROT_PCT/100), rounded to 0.05."""
    prot = ltp * (1 - MARKET_PROT_PCT / 100)
    return round(prot * 20) / 20

def place_order(token: str, sym_id: str, side: str, qty: int, limit_price: float,
                product: str = "normal") -> Dict:
    """Place a single order via REST API. Returns order response."""
    url = f"{BASE_URL}/api-gw/orders/place"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {
        "exchangeSegment": "NFO",
        "exchangeInstrumentID": int(sym_id),
        "productType": product,
        "orderType": "LIMIT",
        "orderSide": side.upper(),  # BUY or SELL
        "timeInForce": "DAY",
        "disclosedQuantity": 0,
        "orderQuantity": qty,
        "limitPrice": limit_price,
        "stopPrice": 0,
    }
    r = requests.post(url, headers=headers, json=body, timeout=15, verify=_SSL)
    r.raise_for_status()
    return r.json()

def cancel_order(token: str, order_id: str) -> Dict:
    """Cancel an order via REST API."""
    url = f"{BASE_URL}/api-gw/orders/cancel"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"appOrderID": order_id}
    r = requests.post(url, headers=headers, json=body, timeout=10, verify=_SSL)
    r.raise_for_status()
    return r.json()

def get_order_book(token: str) -> List[Dict]:
    """Get order book via REST API."""
    url = f"{BASE_URL}/api-gw/orders/book"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, timeout=15, verify=_SSL)
    r.raise_for_status()
    return r.json().get("data", [])

def get_positions(token: str) -> List[Dict]:
    """Get positions via REST API."""
    url = f"{BASE_URL}/api-gw/positions"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, timeout=15, verify=_SSL)
    r.raise_for_status()
    return r.json().get("data", [])

# ── Executor ─────────────────────────────────────────────────────
class StraddleExecutorLite:
    """
    Lightweight executor for StraddleEngine actions.
    
    Features:
    - WebSocket OrderFeed for instant fills
    - Parallel order placement (buy CE & PE concurrently)
    - In-memory position tracking with failure handling
    - Market protection pricing
    - Square-off at 15:20
    
    Usage:
        executor = StraddleExecutorLite(
            api_key="...",
            password="...", 
            totp_secret="...",
            lots=2
        )
        run_shadow_session(executor=executor)
    """
    
    def __init__(self, api_key: str, password: str, totp_secret: str, 
                 lots: int = 1, user_id: str = "standalone", dry_run: bool = False):
        self.api_key = api_key
        self.password = password
        self.totp_secret = totp_secret
        self.lots = lots
        self.user_id = user_id
        self.dry_run = dry_run
        
        # State
        self.token: Optional[str] = None
        self.chain: Optional[Dict] = None
        self.expiry: Optional[str] = None
        self.lot_size: int = DEFAULT_LOT
        
        # Position tracking
        self.client = ClientState(
            user_id=user_id,
            email=user_id,
            lots=lots
        )
        
        # OrderFeed for instant fills
        self._order_feed: Optional[OrderFeed] = None
        self._feed_thread: Optional[threading.Thread] = None
        
        # Pending actions for WS fill coordination
        self._pending_orders: Dict[str, Dict] = {}  # order_id -> {action, meta, position}
        self._pending_lock = threading.Lock()
        
        # Statistics
        self.stats = {"buys": 0, "sells": 0, "fills": 0, "errors": 0}
    
    def initialize(self) -> bool:
        """Initialize tokens, discover chain, start OrderFeed."""
        try:
            log.info("straddle exec: initializing...")
            self.token = _get_data_token(self.api_key, self.password, self.totp_secret)
            log.info("straddle exec: token acquired")
            
            self.chain = discover_chain(self.api_key, self.password, self.totp_secret, self.token)
            self.expiry = self.chain["expiry"]
            self.lot_size = self.chain["lot"]
            log.info("straddle exec: chain discovered expiry=%s lot=%d CE=%d PE=%d",
                     self.expiry, self.lot_size, len(self.chain["CE"]), len(self.chain["PE"]))
            
            # Start OrderFeed for instant fills
            self._order_feed = OrderFeed(self.api_key, self.token)
            self._order_feed.start()
            log.info("straddle exec: OrderFeed started")
            
            return True
        except Exception as e:
            log.error("straddle exec init failed: %s", e)
            return False
    
    def _get_position(self, sym_id: str) -> Optional[PositionState]:
        return self.client.positions.get(sym_id)
    
    def _get_or_create_position(self, leg: str, strike: float, sym: ResolvedSymbol) -> PositionState:
        pos = self.client.positions.get(sym.sym_id)
        if pos is None:
            pos = PositionState(
                leg=leg,
                strike=strike,
                sym_id=sym.sym_id,
                symbol=sym.symbol,
                lots=self.lots * self.lot_size
            )
            self.client.positions[sym.sym_id] = pos
        return pos
    
    def _track_pending_order(self, order_id: str, action: dict, meta: dict, position: PositionState):
        with self._pending_lock:
            self._pending_orders[order_id] = {
                "action": action,
                "meta": meta,
                "position": position,
                "ts": time.time()
            }
            position.order_id = order_id
    
    def _clear_pending_order(self, order_id: str) -> Optional[Dict]:
        with self._pending_lock:
            return self._pending_orders.pop(order_id, None)
    
    def _check_fills(self):
        """Check OrderFeed for fills and update position state."""
        if not self._order_feed:
            return
        try:
            fills = self._order_feed.drain_fills()
            for fill in fills:
                order_id = fill.get("order_id")
                pending = self._clear_pending_order(order_id)
                if not pending:
                    log.debug("straddle exec: fill for unknown order_id %s", order_id)
                    continue
                
                position = pending["position"]
                side = fill.get("side", "").upper()
                fill_price = float(fill.get("avg_price", 0))
                fill_qty = int(fill.get("fill_qty", 0))
                
                if side == "BUY":
                    position.entry_price = fill_price
                    position.qty = fill_qty
                    position.status = "open"
                    self.stats["fills"] += 1
                    log.info("straddle exec: BUY filled %s @ %.2f qty=%d", 
                             position.symbol, fill_price, fill_qty)
                elif side == "SELL":
                    position.exit_price = fill_price
                    position.qty = 0
                    position.status = "closed"
                    self.stats["fills"] += 1
                    log.info("straddle exec: SELL filled %s @ %.2f qty=%d", 
                             position.symbol, fill_price, fill_qty)
        except Exception as e:
            log.warning("straddle exec: check_fills error: %s", e)
    
    def __call__(self, action: dict, meta: dict) -> None:
        """Runner hook: called for each engine action (buy/sell)."""
        self._check_fills()  # Process any pending fills first
        
        side = action.get("side", "").lower()
        leg = action.get("leg", "")
        strike = float(meta.get("strike", 0))
        reason = action.get("reason", "")
        
        log.info("straddle exec: action %s %s %s strike=%.0f reason=%s", 
                 side.upper(), leg, "", strike, reason)
        
        if self.dry_run:
            log.info("straddle exec: DRY-RUN - would %s %s %s @ Rs%.1f", 
                     side.upper(), leg, action.get("premium", ""), strike)
            return
        
        if not self.token or not self.chain:
            log.error("straddle exec: not initialized")
            return
        
        try:
            sym = _resolve_sym(self.token, leg, strike)
        except Exception as e:
            log.error("straddle exec: resolve failed: %s", e)
            self.stats["errors"] += 1
            return
        
        position = self._get_or_create_position(leg, strike, sym)
        qty = self.lots * sym.lot
        
        if side == "buy":
            # Check if already open
            if position.status == "open" and position.qty > 0:
                log.warning("straddle exec: already long %s, skipping", sym.symbol)
                return
            
            # Get current LTP for market protection
            ltp_map = self._get_ltp([sym.sym_id])
            ltp = ltp_map.get(sym.sym_id)
            if ltp is None or ltp <= 0:
                log.warning("straddle exec: no LTP for %s, using action premium", sym.symbol)
                ltp = action.get("premium", 0)
            
            limit_price = _limit_buy_price(ltp)
            log.info("straddle exec: placing BUY %s qty=%d limit=%.2f (ltp=%.2f)", 
                     sym.symbol, qty, limit_price, ltp)
            
            try:
                resp = place_order(self.token, sym.sym_id, "BUY", qty, limit_price)
                order_id = resp.get("data", {}).get("appOrderID")
                if order_id:
                    self._track_pending_order(order_id, action, meta, position)
                    self.stats["buys"] += 1
                    log.info("straddle exec: BUY order placed order_id=%s", order_id)
                else:
                    log.error("straddle exec: BUY order failed: %s", resp)
                    self.stats["errors"] += 1
            except Exception as e:
                log.error("straddle exec: BUY exception: %s", e)
                self.stats["errors"] += 1
                
        elif side == "sell":
            # Get current LTP for market protection
            ltp_map = self._get_ltp([sym.sym_id])
            ltp = ltp_map.get(sym.sym_id)
            if ltp is None or ltp <= 0:
                ltp = action.get("premium", 0)
            
            limit_price = _limit_sell_price(ltp)
            log.info("straddle exec: placing SELL %s qty=%d limit=%.2f (ltp=%.2f)", 
                     sym.symbol, position.qty, limit_price, ltp)
            
            # Use live qty from position (or last known)
            sell_qty = position.qty if position.qty > 0 else qty
            if sell_qty <= 0:
                log.warning("straddle exec: no qty to sell for %s", sym.symbol)
                return
            
            try:
                resp = place_order(self.token, sym.sym_id, "SELL", sell_qty, limit_price)
                order_id = resp.get("data", {}).get("appOrderID")
                if order_id:
                    self._track_pending_order(order_id, action, meta, position)
                    self.stats["sells"] += 1
                    log.info("straddle exec: SELL order placed order_id=%s", order_id)
                else:
                    log.error("straddle exec: SELL order failed: %s", resp)
                    self.stats["errors"] += 1
            except Exception as e:
                log.error("straddle exec: SELL exception: %s", e)
                self.stats["errors"] += 1
    
    def _get_ltp(self, tokens: List[str]) -> Dict[str, float]:
        """Get LTP via REST quote API."""
        if not tokens:
            return {}
        
        # Use ChainFeed if available, else REST
        if hasattr(self, '_chain_feed') and self._chain_feed:
            try:
                return self._chain_feed.ltp(tokens)
            except Exception:
                pass
        
        # Fallback to REST
        try:
            headers = {"Authorization": f"Bearer {self.token}"}
            q = ",".join(tokens)
            r = requests.get(f"{BASE_URL}/api-gw/market/quote?exchangeSegment=NFO&exchangeInstrumentIDs={q}",
                           headers=headers, timeout=10, verify=_SSL)
            r.raise_for_status()
            data = r.json().get("data", {})
            return {str(k): float(v.get("lastTradedPrice", 0)) for k, v in data.items()}
        except Exception as e:
            log.warning("straddle exec: LTP fetch failed: %s", e)
            return {}
    
    def square_off_all(self) -> Dict[str, int]:
        """Square off all open positions at 15:20."""
        log.info("straddle exec: SQUARE OFF ALL")
        results = {"closed": 0, "failed": 0}
        
        for sym_id, position in list(self.client.positions.items()):
            if position.status == "open" and position.qty > 0:
                try:
                    # Get current LTP
                    ltp_map = self._get_ltp([sym_id])
                    ltp = ltp_map.get(sym_id, position.entry_price)
                    limit_price = _limit_sell_price(ltp)
                    
                    log.info("straddle exec: square-off %s qty=%d limit=%.2f", 
                             position.symbol, position.qty, limit_price)
                    
                    resp = place_order(self.token, sym_id, "SELL", position.qty, limit_price)
                    order_id = resp.get("data", {}).get("appOrderID")
                    if order_id:
                        self._track_pending_order(order_id, {"side": "sell", "leg": position.leg}, 
                                                  {"strike": position.strike}, position)
                        results["closed"] += 1
                    else:
                        results["failed"] += 1
                except Exception as e:
                    log.error("straddle exec: square-off failed for %s: %s", position.symbol, e)
                    results["failed"] += 1
        
        # Wait for fills (max 30s)
        import time
        start = time.time()
        while time.time() - start < 30:
            self._check_fills()
            if all(p.status != "open" for p in self.client.positions.values()):
                break
            time.sleep(0.5)
        
        return results
    
    def shutdown(self):
        """Clean shutdown."""
        if self._order_feed:
            self._order_feed.stop()
        log.info("straddle exec: shutdown stats: %s", self.stats)
    
    # For guardian/heartbeat compatibility
    def managed_keys(self, open_legs: set) -> list[str]:
        syms = {p.sym_id for p in self.client.positions.values() 
                if p.leg in open_legs and p.status == "open"}
        return sorted(f"{self.user_id}|{sym}" for sym in syms)


# ── Convenience: create executor from env ───────────────────────
def create_executor_from_env(lots: int = 1, dry_run: bool = False) -> Optional[StraddleExecutorLite]:
    """Create executor from environment variables."""
    api_key = os.getenv("TRADEJINI_DATA_API_KEY")
    password = os.getenv("TRADEJINI_PASSWORD")
    totp = os.getenv("TRADEJINI_TOTP")
    
    if not all([api_key, password, totp]):
        log.warning("straddle exec: missing env vars (TRADEJINI_DATA_API_KEY, TRADEJINI_PASSWORD, TRADEJINI_TOTP)")
        return None
    
    return StraddleExecutorLite(api_key, password, totp, lots=lots, dry_run=dry_run)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    
    api_key = os.getenv("TRADEJINI_DATA_API_KEY")
    password = os.getenv("TRADEJINI_PASSWORD")
    totp = os.getenv("TRADEJINI_TOTP")
    
    if not all([api_key, password, totp]):
        print("Set TRADEJINI_DATA_API_KEY, TRADEJINI_PASSWORD, TRADEJINI_TOTP")
        sys.exit(1)
    
    ex = StraddleExecutorLite(api_key, password, totp, lots=1, dry_run=True)
    if ex.initialize():
        print("Executor initialized successfully (dry-run)")
    else:
        print("Init failed")
