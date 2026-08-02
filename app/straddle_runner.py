"""Intraday runner for the options straddle strategy (Phases 2-3).

Orchestrates the live session: at 09:35 it picks the closest-~Rs.50 CE & PE on the
near weekly expiry, then each minute feeds their premiums into the StraddleEngine
and collects buy/sell ACTIONS. Two modes:
  - SHADOW (default): logs what it WOULD trade, places nothing — safe to run live.
  - replay(date): re-runs a historical day from Kite minute candles (validation).

Phase 4 plugs in here: pass an `executor(action, leg_meta)` callback to turn each
engine action into a real per-client Tradejini order. Until then, executor=None.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import time
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.options_straddle import StraddleConfig, StraddleEngine

log = logging.getLogger("straddle")

# Backend-agnostic paths for lock/heartbeat (use current dir if not in aiprosperity)
_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_LOCK_PATH = os.environ.get("STRADDLE_LOCK_PATH", os.path.join(_BACKEND_DIR, ".straddle_runner.lock"))
_HB_PATH = os.environ.get("STRADDLE_HB_PATH", os.path.join(_BACKEND_DIR, ".straddle_heartbeat.json"))
_lock_fp = None


def _acquire_lock() -> bool:
    """Take the exclusive single-instance lock. Returns True if we hold it."""
    global _lock_fp
    if _lock_fp is not None:
        return True
    try:
        import fcntl
    except ImportError:
        # Windows - no flock, just use file existence
        if os.path.exists(_LOCK_PATH):
            return False
        with open(_LOCK_PATH, "w") as fp:
            fp.write(f"{os.getpid()}\n")
        _lock_fp = fp
        return True

    fp = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fp.close()
        return False
    try:
        fp.write(f"{os.getpid()}\n")
        fp.flush()
    except Exception:
        pass
    _lock_fp = fp
    return True


def _write_heartbeat(managed: list, open_legs: set, session: str) -> None:
    """Atomically write the runner heartbeat."""
    try:
        payload = {
            "ts": time.time(),
            "iso": _ist_now().strftime("%Y-%m-%d %H:%M:%S"),
            "pid": os.getpid(),
            "session": session,
            "open_legs": sorted(open_legs),
            "managed": list(managed),
        }
        tmp = f"{_HB_PATH}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            import json
            json.dump(payload, f)
        os.replace(tmp, _HB_PATH)
    except Exception as e:
        log.warning("straddle heartbeat write failed: %s", e)


def _clear_heartbeat() -> None:
    """Remove the heartbeat on a CLEAN session end."""
    try:
        os.remove(_HB_PATH)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("straddle heartbeat clear failed: %s", e)


def _launched_via() -> str:
    if os.environ.get("INVOCATION_ID") or os.environ.get("JOURNAL_STREAM"):
        return "systemd"
    return "manual"


# ── Kite data source (optional, for replay/backtest) ────────────────
_KITE_API = "https://api.kite.trade"
_MIN_GAP = 0.4
_last_call = [0.0]
_CTX = None


def _get_ssl_ctx():
    global _CTX
    if _CTX is None:
        try:
            import certifi
            import ssl
            _CTX = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            try:
                import ssl
                _CTX = ssl.create_default_context()
            except Exception:
                import ssl
                _CTX = ssl._create_unverified_context()
    return _CTX


def _auto_login() -> str:
    """Placeholder - requires Kite credentials in env. Override for replay if needed."""
    # For replay with Kite, set KITE_API_KEY:KITE_ACCESS_TOKEN in env
    token = os.environ.get("KITE_ACCESS_TOKEN", "")
    if token:
        return token
    raise RuntimeError("KITE_ACCESS_TOKEN not set (required for replay/backtest)")


def _kget(path: str) -> bytes:
    gap = _MIN_GAP - (time.monotonic() - _last_call[0])
    if gap > 0:
        time.sleep(gap)
    tok = _auto_login()
    req = urllib.request.Request(
        f"{_KITE_API}{path}",
        headers={"X-Kite-Version": "3",
                 "Authorization": f"token {os.environ.get('KITE_API_KEY', '')}:{tok}"})
    for attempt in range(2):
        try:
            r = urllib.request.urlopen(req, timeout=30, context=_get_ssl_ctx()).read()
            _last_call[0] = time.monotonic()
            return r
        except urllib.error.HTTPError as e:
            _last_call[0] = time.monotonic()
            if e.code == 429 and attempt == 0:
                time.sleep(1.5)
                continue
            raise


def _discover_legs_kite() -> dict:
    """Return {'expiry', 'lot', 'CE': {strike: token}, 'PE': {strike: token}}."""
    rows = list(csv.DictReader(io.StringIO(_kget("/instruments/NFO").decode("utf-8", "ignore"))))
    nopt = [r for r in rows if r.get("name") == "NIFTY" and r.get("instrument_type") in ("CE", "PE")]
    expiry = sorted({r["expiry"] for r in nopt})[0]
    lot = int(nopt[0]["lot_size"])
    exp = [r for r in nopt if r["expiry"] == expiry]
    CE = {float(r["strike"]): r["instrument_token"] for r in exp if r["instrument_type"] == "CE"}
    PE = {float(r["strike"]): r["instrument_token"] for r in exp if r["instrument_type"] == "PE"}
    return {"expiry": expiry, "lot": lot, "CE": CE, "PE": PE}


def _minute_candles(token: str, day_from: str, day_to: str):
    import json
    j = json.loads(_kget(f"/instruments/historical/{token}/minute?from={day_from}&to={day_to}"))
    return [(datetime.strptime(c[0][:19], "%Y-%m-%dT%H:%M:%S"), float(c[4]))
            for c in (j.get("data") or {}).get("candles", [])]


def _ltp_kite(tokens: list) -> dict:
    if not tokens:
        return {}
    out = {}
    for i in range(0, len(tokens), 200):
        batch = tokens[i:i + 200]
        q = "&".join(f"i={t}" for t in batch)
        import json
        j = json.loads(_kget(f"/quote/ltp?{q}"))
        data = j.get("data") or {}
        for t in batch:
            d = data.get(str(t)) or data.get(t)
            if d:
                out[str(t)] = float(d.get("last_price", 0.0))
    return out


# ── Data source selection ──────────────────────────────────────────
_TJ_FEED = None


def _use_tradejini() -> bool:
    return os.environ.get("STRADDLE_DATA_SRC", "tradejini").strip().lower() != "kite"


def discover_legs() -> dict:
    """Chain for the nearest weekly {'expiry','lot','CE':{strike:token},'PE':{strike:token}}."""
    if not _use_tradejini():
        return _discover_legs_kite()
    # Tradejini WS path - requires tradejini_data module to be available
    try:
        from . import tradejini_data as td
    except ImportError:
        log.warning("tradejini_data not available, falling back to Kite")
        return _discover_legs_kite()
    global _TJ_FEED
    chain = td.discover_chain("NIFTY")
    all_ex = list(chain["CE"].values()) + list(chain["PE"].values())
    _TJ_FEED = td.ChainFeed().start(all_ex)
    log.info("straddle data: Tradejini WS feed started over %d strikes (expiry %s, lot %d)",
             len(all_ex), chain["expiry"], chain["lot"])
    return chain


def ltp(tokens: list) -> dict:
    """Live premiums {str(token): ltp}."""
    if not _use_tradejini():
        return _ltp_kite(tokens)
    if _TJ_FEED is None or not tokens:
        return {}
    _TJ_FEED.wait_ready(tokens, 8)
    return _TJ_FEED.ltp(tokens)


def pick_closest_premium(prem_by_strike: dict, target: float) -> Optional[float]:
    best, bd = None, 1e18
    for s, p in prem_by_strike.items():
        if p is not None and abs(p - target) < bd:
            best, bd = s, abs(p - target)
    return best


# ── Runner ─────────────────────────────────────────────────────
class StraddleRunner:
    """One trading day. Pick legs at 09:35, then tick() each minute with premiums."""

    def __init__(self, cfg: StraddleConfig, lot: int, ce_leg: Tuple[float, str],
                 pe_leg: Tuple[float, str], executor=None):
        self.cfg, self.lot = cfg, lot
        self.ce_strike, self.ce_token = ce_leg
        self.pe_strike, self.pe_token = pe_leg
        self.engine = StraddleEngine(cfg)
        self.executor = executor
        self.actions = []

    def tick(self, now: datetime, ce_prem: float, pe_prem: float) -> list:
        acts = self.engine.on_minute(now, {"CE": ce_prem, "PE": pe_prem})
        for a in acts:
            meta = {"strike": self.ce_strike if a["leg"] == "CE" else self.pe_strike,
                    "token": self.ce_token if a["leg"] == "CE" else self.pe_token,
                    "lot": self.lot, "time": now}
            a["meta"] = meta
            self.actions.append((now, a))
            log.info("straddle %s %s %s @Rs%.1f strike=%.0f (%s)%s",
                     a["side"].upper(), a["leg"], "", a["premium"], meta["strike"], a["reason"],
                     "" if self.executor is None else " [LIVE]")
            if self.executor is not None:
                try:
                    self.executor(a, meta)
                except Exception as e:
                    log.warning("straddle executor error: %s", e)
        return acts

    def virtual_pnl(self) -> float:
        return self.engine.day_pnl(self.lot)


# ── Entry points ────────────────────────────────────────────────
def replay(date_str: str) -> dict:
    """Re-run a historical day from Kite minute candles (validation)."""
    chain = discover_legs()
    frm, to = f"{date_str}+09:15:00", f"{date_str}+15:30:00"

    def at935(tokmap, lo, hi):
        out = {}
        for s, tok in tokmap.items():
            if not (lo <= s <= hi):
                continue
            for dtm, p in _minute_candles(tok, frm, to):
                if (dtm.hour, dtm.minute) == (9, 35):
                    out[s] = p
                    break
        return out

    ce935 = at935(chain["CE"], 23400, 24400)
    pe935 = at935(chain["PE"], 22600, 23500)
    ce_s = pick_closest_premium(ce935, 50.0)
    pe_s = pick_closest_premium(pe935, 50.0)
    if ce_s is None or pe_s is None:
        return {"error": "no ~Rs.50 strike found in band"}

    cfg = StraddleConfig()
    r = StraddleRunner(cfg, chain["lot"], (ce_s, chain["CE"][ce_s]), (pe_s, chain["PE"][pe_s]))
    ce_ser = dict(_minute_candles(chain["CE"][ce_s], frm, to))
    pe_ser = dict(_minute_candles(chain["PE"][pe_s], frm, to))
    last_ce = last_pe = None
    for t in sorted(set(ce_ser) | set(pe_ser)):
        last_ce = ce_ser.get(t, last_ce)
        last_pe = pe_ser.get(t, last_pe)
        if last_ce is None or last_pe is None:
            continue
        r.tick(t, last_ce, last_pe)

    return {"date": date_str, "ce_strike": ce_s, "pe_strike": pe_s,
            "lot": chain["lot"], "pnl": r.virtual_pnl(),
            "ce_entries": r.engine.legs["CE"].entries, "pe_entries": r.engine.legs["PE"].entries,
            "n_actions": len(r.actions)}


def _ist_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _in_late_start_refuse_window(start_hm: str) -> bool:
    """True if an IST 'HH:MM' start is a restart/late start that must NOT open a fresh session."""
    return "09:37" <= start_hm <= "15:20"


def _carry(cur, last):
    """Carry the last-known premium across a momentary missing quote."""
    return cur if cur is not None else last


def run_shadow_session(executor=None, make_executor=None, cfg=None):
    """Run ONE live session (IST market hours) - SHADOW mode only in standalone."""
    cfg = cfg or StraddleConfig()
    if not _acquire_lock():
        log.error("straddle: another runner holds the lock (%s) — refusing to start", _LOCK_PATH)
        return None

    log.info("straddle SHADOW session starting (IST %s) — pid=%s launched=%s",
             _ist_now().strftime("%H:%M"), os.getpid(), _launched_via())

    # Late-start guard
    start_hm = _ist_now().strftime("%H:%M")
    if _in_late_start_refuse_window(start_hm) and os.environ.get("STRADDLE_ALLOW_LATE_START") != "1":
        log.error("straddle: started %s IST inside the trading window (restart/late) — "
                  "REFUSING to open a session; guardian will flatten any open leg.", start_hm)
        _clear_heartbeat()
        return None

    chain = discover_legs()
    ce_tokens = list(chain["CE"].values())
    pe_tokens = list(chain["PE"].values())

    # Wait until 09:35 IST
    while _ist_now().time().strftime("%H:%M") < "09:35":
        _write_heartbeat([], set(), "shadow")
        time.sleep(5)

    # 09:35 leg selection
    ce_ltp = ltp(ce_tokens)
    pe_ltp = ltp(pe_tokens)
    ce_prem = {s: ce_ltp.get(str(t)) for s, t in chain["CE"].items() if ce_ltp.get(str(t)) is not None}
    pe_prem = {s: pe_ltp.get(str(t)) for s, t in chain["PE"].items() if pe_ltp.get(str(t)) is not None}
    ce_s = pick_closest_premium(ce_prem, cfg.target_premium)
    pe_s = pick_closest_premium(pe_prem, cfg.target_premium)

    if ce_s is None or pe_s is None:
        log.warning("straddle: could not pick ~Rs.%.0f legs; aborting session", cfg.target_premium)
        return

    log.info("straddle legs: CE %s@Rs%.1f  PE %s@Rs%.1f  (expiry %s lot %d)",
             ce_s, ce_prem[ce_s], pe_s, pe_prem[pe_s], chain["expiry"], chain["lot"])

    runner = StraddleRunner(cfg, chain["lot"], (ce_s, chain["CE"][ce_s]),
                            (pe_s, chain["PE"][pe_s]), executor=executor)

    # Minute loop until 15:20 IST
    last_min = None
    last_cep = last_pep = None
    try:
        while _ist_now().time().strftime("%H:%M") <= "15:20":
            now = _ist_now()
            open_legs = {n for n, lg in runner.engine.legs.items() if lg.in_pos}
            managed = executor.managed_keys(open_legs) if executor is not None else []
            _write_heartbeat(managed, open_legs, "shadow")
            cur = now.strftime("%H:%M")
            if cur != last_min:
                last_min = cur
                try:
                    q = ltp([runner.ce_token, runner.pe_token])
                    cep = q.get(str(runner.ce_token))
                    pep = q.get(str(runner.pe_token))
                    last_cep = _carry(cep, last_cep)
                    last_pep = _carry(pep, last_pep)
                    if last_cep is not None and last_pep is not None:
                        runner.tick(now.replace(second=0, microsecond=0), last_cep, last_pep)
                except Exception as e:
                    log.warning("straddle tick error: %s", e)
                open_legs = {n for n, lg in runner.engine.legs.items() if lg.in_pos}
                managed = executor.managed_keys(open_legs) if executor is not None else []
                _write_heartbeat(managed, open_legs, "shadow")
            time.sleep(10)
    finally:
        _clear_heartbeat()

    log.info("straddle session done [SHADOW]: virtual P&L Rs.%.0f over %d actions (CE %d / PE %d entries)",
             runner.virtual_pnl(), len(runner.actions),
             runner.engine.legs["CE"].entries, runner.engine.legs["PE"].entries)
    return runner


if __name__ == "__main__":
    import sys
    import json
    if len(sys.argv) > 1 and sys.argv[1] == "shadow":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        run_shadow_session()
    else:
        d = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
        print(json.dumps(replay(d), default=str, indent=2))