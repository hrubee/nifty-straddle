"""Tradejini (Indian F&O) — per-client OAuth + trading client.

Multi-tenant model: each client authorizes via Tradejini's hosted login
(cubeplus SSO) — they never hand us their password. We exchange the returned
code for a per-client access token and act on their account with the bearer
``<app_key>:<access_token>`` (the format the official SDK mandates).

Tradejini tokens EXPIRE (≈ daily, no refresh grant) — clients re-connect each
trading day. `expires_in` (seconds) is stored so we can prompt before expiry.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import ssl
import struct
import time
import urllib.parse
from typing import Any

import requests

import config


def _ctx() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        try:
            return ssl.create_default_context()
        except Exception:
            return ssl._create_unverified_context()


_SSL = _ctx()

# Singleton session with connection pooling (keep-alive + reuse)
# Created lazily on first TradejiniClient instantiation.
_SESSION: requests.Session | None = None
_ADAPTER: requests.adapters.HTTPAdapter | None = None


def _get_session() -> requests.Session:
    """Return a shared requests.Session with connection pooling."""
    global _SESSION, _ADAPTER
    if _SESSION is None:
        _SESSION = requests.Session()
        _ADAPTER = requests.adapters.HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=0,
            pool_block=False,
        )
        _SESSION.mount("https://", _ADAPTER)
        _SESSION.mount("http://", _ADAPTER)
        # Default timeout for all requests (can be overridden per-call)
        _SESSION.request = _timeout_wrapper(_SESSION.request, 20)
    return _SESSION


def _timeout_wrapper(func, default_timeout):
    """Wrap Session.request to enforce a default timeout if none provided."""
    def wrapped(*args, **kwargs):
        if "timeout" not in kwargs:
            kwargs["timeout"] = default_timeout
        return func(*args, **kwargs)
    return wrapped


class TradejiniError(Exception):
    pass


# ── OAuth ──────────────────────────────────────────────────────
def authorize_url(state: str) -> str:
    """The URL to send a client to so they log in on Tradejini and authorize us."""
    q = urllib.parse.urlencode({
        "client_id": config.settings.tradejini_app_key,
        "redirect_uri": config.settings.tradejini_redirect_uri,
        "response_type": "code",
        "scope": "general",
        "state": state,
    })
    return f"{config.settings.tradejini_base_url}/api-gw/oauth/authorize?{q}"


def exchange_code(code: str) -> dict:
    """Exchange the redirect ``code`` for a client access token."""
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": config.settings.tradejini_app_key,
        "redirect_uri": config.settings.tradejini_redirect_uri,
        "client_secret": config.settings.tradejini_app_secret,
        "grant_type": "authorization_code",
    }).encode()
    # OAuth token exchange uses urllib directly (one-off, no pooling needed)
    req = urllib.request.Request(
        f"{config.settings.tradejini_base_url}/api-gw/oauth/token", data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=20, context=_SSL).read())
    except urllib.error.HTTPError as e:
        raise TradejiniError(f"token exchange HTTP {e.code}: {e.read().decode()[:200]}")
    except Exception as e:
        raise TradejiniError(f"token exchange error: {e}")
    token = resp.get("access_token")
    if not token:
        raise TradejiniError(f"no access_token in response: {resp}")
    return {"access_token": token, "expires_in": int(resp.get("expires_in") or 0),
            "scope": resp.get("scope"), "token_type": resp.get("token_type")}


def verify_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
    """Tradejini signs order-event postbacks with HMAC-SHA256(api_secret, body),
    hex digest, in the X-SIGNATURE header (a trailing '~' delimiter may be
    appended). Returns True only on an exact constant-time match."""
    if not signature:
        return False
    secret = (config.settings.tradejini_app_secret or "").encode()
    if not secret:
        return False
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    got = signature.strip().strip("~").strip().lower()
    return hmac.compare_digest(expected, got)


# ── data-account auto-login (TOTP) ─────────────────────────────
def _totp_now(secret_b32: str) -> str:
    """6-digit TOTP from a base32 seed (RFC 6238, SHA1, 30s window)."""
    s = secret_b32.strip().replace(" ", "").upper()
    s += "=" * ((8 - len(s) % 8) % 8)
    key = base64.b32decode(s)
    counter = int(time.time()) // 30
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


_data_cache: dict = {"key": None, "token": None, "exp": 0.0, "fail_until": 0.0}
# After a failed login, back off this long before trying again — a repeatedly
# failing auto-login (e.g. IP not yet whitelisted) must NOT hammer the broker
# auth endpoint each cycle, which would lock the account.
_LOGIN_FAIL_COOLDOWN = 1800  # 30 min


def individual_data_token() -> tuple[str, str] | None:
    """(api_key, access_token) for the brain's DATA account via auto-login
    (api_key + password + TOTP). Cached until ~5 min before expiry. Returns None
    when the data creds aren't configured OR we're in post-failure cooldown
    (caller falls back to a client token)."""
    key = config.settings.tradejini_data_api_key.strip()
    pw = config.settings.tradejini_data_password
    seed = config.settings.tradejini_data_totp_secret.strip()
    if not (key and pw and seed):
        return None
    now = time.time()
    if _data_cache["token"] and now < _data_cache["exp"] - 300:
        return _data_cache["key"], _data_cache["token"]
    if now < _data_cache.get("fail_until", 0):
        return None  # cooling down after a recent failure — fall back, don't hammer
    body = urllib.parse.urlencode({"password": pw, "twoFa": _totp_now(seed), "twoFaTyp": "totp"}).encode()
    # individual-token-v2 uses urllib directly (one-off, infrequent)
    req = urllib.request.Request(
        f"{config.settings.tradejini_base_url}/api-gw/oauth/individual-token-v2", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=20, context=_SSL).read())
    except urllib.error.HTTPError as e:
        _data_cache["fail_until"] = now + _LOGIN_FAIL_COOLDOWN
        raise TradejiniError(f"individual-token HTTP {e.code}: {e.read().decode()[:200]}")
    except Exception as e:
        _data_cache["fail_until"] = now + _LOGIN_FAIL_COOLDOWN
        raise TradejiniError(f"individual-token error: {e}")
    tok = resp.get("access_token")
    if not tok:
        _data_cache["fail_until"] = now + _LOGIN_FAIL_COOLDOWN
        raise TradejiniError(f"no access_token in individual-token response: {resp}")
    _data_cache.update(key=key, token=tok, exp=now + int(resp.get("expires_in") or 86400), fail_until=0.0)
    return key, tok


def mint_client_token(api_key: str, password: str, two_fa: str,
                      two_fa_typ: str = "totp") -> tuple[str, int]:
    """Per-CLIENT direct login (Tradejini's only multi-tenant path — there is NO
    OAuth/SSO). Mirrors `individual_data_token` but for a client's own api_key +
    the password and TOTP/OTP code they enter at daily reconnect. Returns
    (access_token, expires_in_seconds). No caching, no password storage — called
    on demand from /tradejini/reauth. The authorizing header is `Bearer <api_key>`
    (the SDK's authKey); all subsequent calls use `<api_key>:<access_token>`."""
    api_key = (api_key or "").strip()
    if not (api_key and password and two_fa):
        raise TradejiniError("api_key, password and 2FA code are all required")
    body = urllib.parse.urlencode({"password": password, "twoFa": two_fa,
                                   "twoFaTyp": (two_fa_typ or "totp")}).encode()
    # Individual token uses urllib directly (one-off, infrequent)
    req = urllib.request.Request(
        f"{config.settings.tradejini_base_url}/api-gw/oauth/individual-token-v2", data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=20, context=_SSL).read())
    except urllib.error.HTTPError as e:
        raise TradejiniError(f"individual-token HTTP {e.code}: {e.read().decode()[:200]}")
    except Exception as e:
        raise TradejiniError(f"individual-token error: {e}")
    tok = resp.get("access_token")
    if not tok:
        raise TradejiniError(f"no access_token in response: {resp}")
    return tok, int(resp.get("expires_in") or 86400)


# ── per-client trading/data client ─────────────────────────────
class TradejiniClient:
    """Trading client using shared requests.Session with connection pooling.

    All HTTP calls reuse TCP connections via the session's adapter pool,
    eliminating per-request TLS handshake overhead.
    """
    def __init__(self, access_token: str, api_key: str | None = None):
        self.access_token = access_token
        # OAuth client tokens authorize with the APP key; data auto-login tokens
        # authorize with the data account's own api key.
        self.api_key = api_key or config.settings.tradejini_app_key
        self._session = _get_session()
        self._base_url = config.settings.tradejini_base_url

    def _headers(self, content_type: str | None = None) -> dict:
        # Tradejini's authorizing bearer is "<apiKey>:<accessToken>".
        h = {"Authorization": f"Bearer {self.api_key}:{self.access_token}",
             "Accept": "application/json"}
        if content_type:
            h["Content-Type"] = content_type
        return h

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 data: dict | bytes | None = None,
                 headers: dict | None = None,
                 timeout: int = 20) -> Any:
        """Unified request helper using pooled session."""
        url = f"{self._base_url}{path}"
        h = self._headers()
        if headers:
            h.update(headers)
        try:
            resp = self._session.request(
                method, url, params=params, data=data, headers=h, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            body = e.response.text[:160] if e.response is not None else "no response"
            raise TradejiniError(f"{method} {path} HTTP {e.response.status_code}: {body}")
        except requests.RequestException as e:
            raise TradejiniError(f"{method} {path} error: {e}")

    def _get(self, path: str, params: dict | None = None) -> Any:
        return self._request("GET", path, params=params)

    def _post(self, path: str, data: dict, content_type: str = "application/x-www-form-urlencoded") -> Any:
        body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
        return self._request("POST", path, data=body, headers={"Content-Type": content_type})

    def _put(self, path: str, data: dict) -> Any:
        body = urllib.parse.urlencode(data).encode()
        return self._request("PUT", path, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})

    def _delete(self, path: str, params: dict | None = None) -> Any:
        return self._request("DELETE", path, params=params)

    def validate(self) -> bool:
        """Confirm the token works (used right after connect)."""
        self.equity_inr()
        return True

    def equity_inr(self) -> float:
        """Available margin / cash on the client's Tradejini account (INR)."""
        j = self._get("/api/oms/limits")
        if isinstance(j, dict) and j.get("s") not in (None, "ok"):
            raise TradejiniError(f"limits returned: {j}")
        d = (j or {}).get("d", {}) if isinstance(j, dict) else {}
        for k in ("availMargin", "availableBalance", "availBalance", "cash", "net"):
            if k in d:
                try:
                    return float(d[k])
                except (TypeError, ValueError):
                    pass
        return 0.0

    def buyable_cash_inr(self) -> float:
        """Cash usable to BUY options (INR) = total available margin MINUS pledged
        collateral. Option-buy premium must be paid from cash — settled cash AND
        same-day pay-in both count (PROVEN live 2026-06-02: a same-day ₹10k pay-in
        filled a buy while `availCash` read 0) — but pledged stock collateral can
        NOT pay it. Reading only `availCash` UNDERCOUNTS (it omits same-day pay-in),
        so size off (availMargin − collateral): it captures cash + pay-in, can't
        double-count cleared funds, and correctly excludes collateral."""
        j = self._get("/api/oms/limits")
        if isinstance(j, dict) and j.get("s") not in (None, "ok"):
            raise TradejiniError(f"limits returned: {j}")
        d = (j or {}).get("d", {}) if isinstance(j, dict) else {}

        def f(k: str) -> float:
            try:
                return float(d.get(k) or 0)
            except (TypeError, ValueError):
                return 0.0

        usable = (f("availMargin") - f("stockCollateral")
                  - f("brkCollatAmount") - f("auxCollatAmount"))
        return max(0.0, usable)

    def open_positions(self) -> list[dict]:
        j = self._get("/api/oms/positions", {"symDetails": "true"})
        if isinstance(j, dict) and j.get("s") == "no-data":
            return []
        rows = (j or {}).get("d", []) if isinstance(j, dict) else []
        out = []
        for raw in rows:
            try:
                net = float(raw.get("netQty", 0) or 0)
            except (TypeError, ValueError):
                net = 0.0
            if abs(net) < 1e-9:
                continue
            sym_obj = raw.get("sym") or {}
            sym_id = raw.get("symId") or sym_obj.get("id") or ""
            sym = sym_obj.get("tradSymbol") or sym_id
            out.append({"sym_id": sym_id, "symbol": sym, "size": abs(net),
                        "side": "buy" if net > 0 else "sell"})
        return out

    def day_realized_pnl_inr(self) -> float | None:
        """Best-effort booked/realized P&L for today's NIFTY-option positions (INR)
        — the LIVE number to compare against the engine's virtual day_pnl so the
        slippage figure that gates client rollout is measured, not eyeballed.
        Does NOT filter on netQty (a closed leg is flat but still carries the day's
        realized P&L). Returns None when the broker doesn't expose the field — then
        the operator reads it from the Tradejini contract note manually."""
        j = self._get("/api/oms/positions", {"symDetails": "true"})
        if isinstance(j, dict) and j.get("s") == "no-data":
            return 0.0
        rows = (j or {}).get("d", []) if isinstance(j, dict) else []
        total, found = 0.0, False
        for raw in rows:
            sym_obj = raw.get("sym") or {}
            sym = str(sym_obj.get("tradSymbol") or raw.get("symId") or "").upper()
            if "NIFTY" not in sym or any(x in sym for x in ("BANK", "FIN", "MIDCP")):
                continue
            for k in ("realizedPnl", "realisedPnl", "rpnl", "realized", "bookedPnl", "realPnl"):
                if k in raw:
                    try:
                        total += float(raw[k]); found = True
                        break
                    except (TypeError, ValueError):
                        pass
        return total if found else None

    # ── scrip resolution (shared reference data) ───────────────
    def list_scrips(self, group: str) -> list[dict]:
        rows = _scrip_cache.get(group)
        if rows is not None:
            return rows
        j = self._get(f"/api/mkt-data/scrips/symbol-store/{group}")
        rows = (j or {}).get("d", j) if isinstance(j, dict) else j
        rows = rows if isinstance(rows, list) else []
        _scrip_cache[group] = rows
        return rows

    def resolve(self, symbol: str) -> dict:
        """NSE/NFO symbol (e.g. NIFTY26JUNFUT) → {sym_id, lot_size, instrument}."""
        s = symbol.upper().strip()
        p = _parse_fno(s)
        groups = ["FutureContracts"] if p["kind"] == "FUT" else \
                 (["NSEOptions", "BSEOptions"] if p["kind"] == "OPT" else
                  ["FutureContracts", "NSEOptions", "Securities"])
        for group in groups:
            try:
                rows = self.list_scrips(group)
            except TradejiniError:
                continue
            for sc in rows:  # direct id / dispName match
                disp = str(sc.get("dispName", "")).upper()
                if str(sc.get("id", "")).upper() == s or disp.replace(" ", "") == s or str(sc.get("excToken", "")) == s:
                    return _scrip_to_meta(sc)
            if p["kind"] in ("FUT", "OPT"):
                for sc in rows:
                    if str(sc.get("symbol", "")).upper() != p["und"]:
                        continue
                    ey, em = _expiry_ym(sc.get("expiry", ""))
                    if ey != p["year"] or em != p["month"]:
                        continue
                    if p["kind"] == "OPT":
                        if str(sc.get("optType", "")).upper() != p["opt"]:
                            continue
                        try:
                            if abs(float(sc.get("strike") or 0) - p["strike"]) > 1e-6:
                                continue
                        except (TypeError, ValueError):
                            continue
                    return _scrip_to_meta(sc)
        raise TradejiniError(f"Tradejini: could not resolve symbol {symbol}")

    def resolve_weekly_option(self, underlying: str, expiry_yyyy_mm_dd: str,
                              strike: float, opt_type: str) -> dict:
        """Resolve a WEEKLY option to {sym_id, lot_size, symbol} by EXACT expiry
        date. `resolve()` matches only year+month, which is ambiguous for weeklies
        (several expiries share a month) — the straddle MUST hit the specific
        weekly it priced on Kite, so we match the full YYYY-MM-DD."""
        und = (underlying or "").upper().strip()
        opt = (opt_type or "").upper().strip()
        want = str(expiry_yyyy_mm_dd)[:10]
        for group in ("NSEOptions", "BSEOptions"):
            try:
                rows = self.list_scrips(group)
            except TradejiniError:
                continue
            for sc in rows:
                if str(sc.get("symbol", "")).upper() != und:
                    continue
                if str(sc.get("optType", "")).upper() != opt:
                    continue
                if str(sc.get("expiry", ""))[:10] != want:
                    continue
                try:
                    if abs(float(sc.get("strike") or 0) - float(strike)) > 1e-6:
                        continue
                except (TypeError, ValueError):
                    continue
                meta = _scrip_to_meta(sc)
                meta["symbol"] = sc.get("dispName") or sc.get("id") or f"{und}{strike:.0f}{opt}"
                return meta
        raise TradejiniError(
            f"Tradejini: could not resolve {und} {want} {strike:.0f} {opt}")

    # ── orders ─────────────────────────────────────────────────
    def place_order(self, sym_id: str, side: str, qty: int, product: str = "normal",
                    order_type: str = "market", limit_price: float = 0.0,
                    trig_price: float = 0.0, mkt_prot: float = 0.0) -> str:
        data = {"product": product, "qty": int(qty), "side": side.lower(),
                "symId": sym_id, "type": order_type.lower(), "validity": "day"}
        if order_type.lower() in ("limit", "stoplimit") and limit_price > 0:
            data["limitPrice"] = limit_price
        if order_type.lower() in ("stopmarket", "stoplimit") and trig_price > 0:
            data["trigPrice"] = trig_price
        # Market-order protection % is MANDATORY for API market orders — Tradejini
        # rejects "Market protection mandatory for all market order" without it.
        # Always send it for market/stopmarket: explicit value if given, else the
        # configured default. Caps how far from LTP the order may fill (slippage
        # guard on the strategy's market entries/exits/square-offs).
        if order_type.lower() in ("market", "stopmarket"):
            data["mktProt"] = mkt_prot if mkt_prot > 0 else config.settings.tradejini_mkt_prot_pct
        j = self._post("/api/oms/place-order", data)
        if j.get("s") != "ok":
            raise TradejiniError(f"order rejected: {j}")
        return str((j.get("d") or {}).get("orderId", ""))

    def modify_order(self, sym_id: str, order_id: str, qty: int,
                     order_type: str = "limit", limit_price: float = 0.0,
                     trig_price: float = 0.0, mkt_prot: float = 0.0) -> str:
        data = {"symId": sym_id, "orderId": order_id, "qty": int(qty),
                "type": order_type.lower(), "validity": "day"}
        if order_type.lower() in ("limit", "stoplimit") and limit_price > 0:
            data["limitPrice"] = limit_price
        if order_type.lower() in ("stopmarket", "stoplimit") and trig_price > 0:
            data["trigPrice"] = trig_price
        if order_type.lower() in ("market", "stopmarket"):
            data["mktProt"] = mkt_prot if mkt_prot > 0 else config.settings.tradejini_mkt_prot_pct
        j = self._put("/api/oms/modify-order", data)
        if not j.get("status"):
            raise RuntimeError(j.get("message", "modify-order failed"))
        return str(j.get("data", {}).get("orderId", order_id))

    def place_stop_loss(self, sym_id: str, position_side: str, qty: int, trig_price: float) -> str:
        """Protective SL-M on the OPPOSITE side of the position (NSE F&O has no
        reduce-only flag — this is a standalone stop that must be cancelled on
        close to avoid an orphan reversing the position)."""
        close_side = "buy" if position_side in ("short", "sell") else "sell"
        return self.place_order(sym_id, close_side, int(qty), order_type="stopmarket",
                                trig_price=float(trig_price))

    def list_open_stop_orders(self, sym_id: str) -> list[dict]:
        """Pending stop (SL) orders for `sym_id` — the orphans to cancel on close."""
        j = self._get("/api/oms/orders", {"symDetails": "true"})
        if isinstance(j, dict) and j.get("s") == "no-data":
            return []
        rows = (j or {}).get("d", []) if isinstance(j, dict) else []
        out = []
        for o in rows:
            o_sym = o.get("symId") or (o.get("sym") or {}).get("id") or ""
            if o_sym != sym_id:
                continue
            if str(o.get("status", "")).lower() != "open":
                continue
            if str(o.get("type", "")).lower() in ("stopmarket", "stoplimit") or \
               str(o.get("legType", "")).lower() == "stoploss":
                oid = o.get("orderId")
                if oid:
                    out.append({"order_id": str(oid)})
        return out

    def get_order_status(self, order_id: str) -> str | None:
        """Lower-cased status of one order from the order book, or None if it is
        no longer present. Used to CONFIRM a cancel actually took — a resting buy
        limit can still fill later in the session, so "no position yet" is not
        proof the order is gone."""
        j = self._get("/api/oms/orders", {"symDetails": "true"})
        if isinstance(j, dict) and j.get("s") == "no-data":
            return None
        rows = (j or {}).get("d", []) if isinstance(j, dict) else []
        for o in rows:
            if str(o.get("orderId")) == str(order_id):
                return str(o.get("status", "")).lower()
        return None

    def cancel_order(self, order_id: str) -> bool:
        j = self._delete("/api/oms/cancel-order", params={"orderId": order_id})
        return j.get("s") == "ok"

    def cancel_stop_orders(self, sym_id: str) -> int:
        """Cancel all pending stop orders for `sym_id`. Best-effort; returns count."""
        n = 0
        for o in self.list_open_stop_orders(sym_id):
            try:
                if self.cancel_order(o["order_id"]):
                    n += 1
            except TradejiniError:
                pass
        return n

    def close_position(self, sym_id: str, product: str = "normal", limit_price: float | None = None) -> dict:
        """Flatten the position in `sym_id`: cancel any protective stop first (so
        it can't orphan-reverse), then place the opposite order. `product`
        MUST match the open product to net the position (straddle callers pass
        config.settings.straddle_product); defaults to "normal" for other callers.

        If limit_price is provided, places an IOC LIMIT order at that price for
        bounded slippage. Otherwise uses market order with market protection."""
        cancelled = self.cancel_stop_orders(sym_id)
        for pos in self.open_positions():
            if pos["sym_id"] == sym_id:
                opp = "sell" if pos["side"] == "buy" else "buy"
                if limit_price is not None and limit_price > 0:
                    oid = self.place_order(sym_id, opp, int(pos["size"]), product=product,
                                           order_type="limit", limit_price=limit_price)
                else:
                    oid = self.place_order(sym_id, opp, int(pos["size"]), product=product)
                return {"closed": True, "order_id": oid, "stops_cancelled": cancelled}
        return {"closed": False, "reason": "no position", "stops_cancelled": cancelled}


# ── module-level scrip cache + symbol parsing ──────────────────
_scrip_cache: dict[str, list] = {}
_MONTHS3 = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


def _parse_fno(symbol: str) -> dict:
    import re
    s = symbol.upper().strip()
    m = re.match(r"^([A-Z&\-]+?)(\d{2})([A-Z]{3})FUT$", s)
    if m:
        return {"kind": "FUT", "und": m.group(1), "year": 2000 + int(m.group(2)),
                "month": _MONTHS3.get(m.group(3), 0)}
    m = re.match(r"^([A-Z&\-]+?)(\d{2})([A-Z]{3})(\d+(?:\.\d+)?)(CE|PE)$", s)
    if m:
        return {"kind": "OPT", "und": m.group(1), "year": 2000 + int(m.group(2)),
                "month": _MONTHS3.get(m.group(3), 0), "strike": float(m.group(4)), "opt": m.group(5)}
    return {"kind": "RAW", "und": s}


def _expiry_ym(expiry: str):
    from datetime import datetime as _dt
    try:
        d = _dt.strptime(str(expiry)[:10], "%Y-%m-%d")
        return d.year, d.month
    except Exception:
        return 0, 0


def _scrip_to_meta(sc: dict) -> dict:
    try:
        lot = int(float(sc.get("lot") or 0))
    except (TypeError, ValueError):
        lot = 0
    return {"sym_id": sc.get("id"), "lot_size": lot, "instrument": sc.get("instrument", "")}