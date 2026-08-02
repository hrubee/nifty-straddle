"""Execution-fix unit tests (pre-live gate, 2026-06).

Pins the 8 hardening fixes that sit between the StraddleEngine (strategy — NOT
touched) and real Tradejini orders. The strategy logic lives in
``options_straddle.py`` and is deliberately untested here; these tests guard the
ORDER PATH — the part that can turn a long into a naked short or leave a leg
running past close. Two layers:

  PURE (run anywhere, no deps):
    - ``_in_late_start_refuse_window`` boundaries (Fix #3) — a restart inside the
      trading window must REFUSE a fresh session (the 2026-06-03 root cause). A
      boundary bug here either silently blocks the daily start or fails to block a
      restart, so every boundary minute is asserted explicitly.
    - ``_carry`` (Fix #4) — carry the last premium across a one-minute quote gap so
      a stop / the 15:20 square-off is never skipped; a real 0.0 is NOT a gap.

  DB-BACKED (skipped if sqlalchemy absent — run on the backend venv):
    - ``_sell_one`` close-FAILURE keeps the row OPEN (Fix #2) — a rejected close
      must never mark the books flat while the broker is still LONG.
    - ``reconcile_on_restart`` error-row recovery (Fix #7a) — an order marked
      ``error`` that actually FILLED is recovered to ``open`` so it is squared.
    - ``square_off_all`` dedup (Fix #1) — one close per sym_id even when the broker
      returns two rows for the same contract (a 2nd close = double-sell = naked
      short).
    - square-off single-instance lock (Fix #1) — two concurrent square-offs can't
      both read the same netQty and double-sell.

Run:  ../../.venv/bin/python tests/test_straddle_fixes.py            (pure only)
  or:  <backend-venv>/bin/python tests/test_straddle_fixes.py        (full)
  or:  <backend-venv>/bin/python -m pytest tests/test_straddle_fixes.py
"""
import contextlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import straddle_runner  # noqa: E402


# ════════════════════════════════════════════════════════════════
#  PURE — Fix #3 restart-refuse predicate (HIGHEST value: it gates
#  whether the daily session opens at all)
# ════════════════════════════════════════════════════════════════
def test_late_start_refuse_window_boundaries():
    f = straddle_runner._in_late_start_refuse_window
    # before the window → PROCEED (a normal timer fires ~09:33; the 09:35 wait
    # loop then arms). The grace runs to 09:36 so a start in the arming minute or
    # one minute late still opens normally.
    for hm in ("00:01", "08:00", "09:14", "09:15", "09:30", "09:33", "09:35", "09:36"):
        assert f(hm) is False, f"{hm} should PROCEED (normal/early start)"
    # inside the window [09:37, 15:20] → REFUSE (restart/late — the 09:35 arming
    # reference can't be replayed; a 2nd runner re-arms DIFFERENT strikes).
    for hm in ("09:37", "09:38", "10:00", "12:04", "14:59", "15:19", "15:20"):
        assert f(hm) is True, f"{hm} should REFUSE (restart inside trading window)"
    # at/after the 15:20 square-off → PROCEED (nothing left to protect; the
    # backstop owns EOD). Also late-evening manual runs.
    for hm in ("15:21", "15:25", "15:30", "16:00", "23:52", "23:59"):
        assert f(hm) is False, f"{hm} should PROCEED (post-square-off / off-hours)"
    print("ok: late-start refuse window boundaries (09:37–15:20 refuse, else proceed)")


# ════════════════════════════════════════════════════════════════
#  PURE — Fix #4 quote-gap carry
# ════════════════════════════════════════════════════════════════
def test_carry_quote_gap():
    c = straddle_runner._carry
    assert c(12.5, None) == 12.5          # fresh quote, nothing to carry
    assert c(12.5, 9.0) == 12.5           # fresh quote WINS over last-known
    assert c(None, 9.0) == 9.0            # missing quote → carry last-known
    assert c(None, None) is None          # never seen one yet → still None
    # a REAL 0.0 premium is a value, NOT a gap — must be kept, never replaced by a
    # stale last. (ltp() omits a truly-missing token → None; a present-but-zero
    # quote is a genuine near-worthless leg.)
    assert c(0.0, 9.0) == 0.0
    print("ok: carry keeps fresh quote, carries on None, treats 0.0 as real")


# ════════════════════════════════════════════════════════════════
#  DB-BACKED — shared in-memory harness
# ════════════════════════════════════════════════════════════════
def _try_sqlalchemy():
    try:
        from sqlalchemy import create_engine  # noqa: F401
        return True
    except Exception:
        return False


def _session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models import Base
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)()


def _fake_scope(session):
    """A session_scope() replacement that yields our persistent in-memory session
    and COMMITS but never CLOSES it (a closed :memory: connection drops the data,
    and we still need it for assertions)."""
    @contextlib.contextmanager
    def _scope():
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
    return _scope


def _user_with_tj(db, email="samir@example.com"):
    from app.models import TradejiniConnection, User
    u = User(email=email, role="user")
    db.add(u); db.flush()
    # has_auto_creds = api_key + password_encrypted + totp_seed_encrypted all set
    db.add(TradejiniConnection(user_id=u.id, api_key="k", password_encrypted="pw",
                               totp_seed_encrypted="seed", access_token_encrypted="tok",
                               status="connected", paused=False))
    db.commit()
    return db.get(User, u.id)


class _FakeClient:
    """Broker stand-in. `positions` is what open_positions() returns;
    `close_behaviour` is a callable(sym_id, **kwargs) -> dict | raises."""
    def __init__(self, positions=None, close_behaviour=None):
        self._pos = positions or []
        self._close = close_behaviour or (lambda sid, **kw: {"closed": True, "order_id": "x"})
        self.close_calls = []

    def open_positions(self):
        return self._pos

    def close_position(self, sym_id, product="normal", limit_price=None):
        self.close_calls.append((sym_id, product, limit_price))
        return self._close(sym_id, product=product, limit_price=limit_price)


# ── Fix #2: a rejected close keeps the row OPEN (never books flat) ──
def test_sell_one_close_failure_keeps_row_open():
    if not _try_sqlalchemy():
        print("skip: _sell_one close-failure (sqlalchemy absent)")
        return
    from app import straddle_executor as ex_mod
    from app import telegram_notify
    from app.models import StraddlePosition
    from app.straddle_executor import StraddleClientExecutor

    db = _session()
    user = _user_with_tj(db)
    today = ex_mod._ist_today()
    row = StraddlePosition(user_id=user.id, trade_date=today, leg="PE", strike=22800.0,
                           expiry="2026-06-09", sym_id="SPE", symbol="NIFTY09JUN22800PE",
                           qty=65, status="open", entry_px=50.0)
    db.add(row); db.commit()
    rid = row.id

    sent = []
    orig_scope = ex_mod.session_scope
    orig_send = telegram_notify.send_message
    ex_mod.session_scope = _fake_scope(db)
    telegram_notify.send_message = lambda m, *a, **k: sent.append(m)
    try:
        e = StraddleClientExecutor.__new__(StraddleClientExecutor)
        e.dry_run = False
        e._symcache = {}
        conn = db.get(type(user), user.id)  # any non-None stand-in for _conn_for
        # a close that RAISES (e.g. market-protection reject in a fast move)
        bad = _FakeClient(close_behaviour=lambda sid: (_ for _ in ()).throw(
            RuntimeError("market protection tripped")))
        e._conn_for = lambda db_, uid: conn
        e._client = lambda db_, c: bad
        e._sell_one(s={"user_id": user.id, "email": user.email}, leg="PE",
                    strike=22800.0, sym={"sym_id": "SPE", "symbol": "NIFTY09JUN22800PE"},
                    action={"premium": 30.0, "reason": "stop"})
    finally:
        ex_mod.session_scope = orig_scope
        telegram_notify.send_message = orig_send

    db.expire_all()
    r2 = db.get(StraddlePosition, rid)
    assert r2.status == "open", f"close FAILED must keep row OPEN, got {r2.status}"
    assert r2.exit_px is None, "must not stamp an exit on a failed close"
    assert "sell-FAILED" in (r2.detail or ""), r2.detail
    # close_calls now includes limit_price as 3rd element
    assert bad.close_calls == [("SPE", "normal", 29.7)], bad.close_calls
    assert sent and "CLOSE FAILED" in sent[0], sent
    print("ok: _sell_one close-failure keeps row OPEN + alerts (no phantom flat)")


def test_sell_one_close_success_marks_closed():
    """Control: a clean close DOES mark the row closed (proves the failure test
    isn't passing because nothing happens)."""
    if not _try_sqlalchemy():
        print("skip: _sell_one close-success (sqlalchemy absent)")
        return
    from app import straddle_executor as ex_mod
    from app.models import StraddlePosition
    from app.straddle_executor import StraddleClientExecutor

    db = _session()
    user = _user_with_tj(db)
    today = ex_mod._ist_today()
    row = StraddlePosition(user_id=user.id, trade_date=today, leg="CE", strike=23800.0,
                           expiry="2026-06-09", sym_id="SCE", symbol="NIFTY09JUN23800CE",
                           qty=65, status="open", entry_px=50.0)
    db.add(row); db.commit()
    rid = row.id

    orig_scope = ex_mod.session_scope
    ex_mod.session_scope = _fake_scope(db)
    try:
        e = StraddleClientExecutor.__new__(StraddleClientExecutor)
        e.dry_run = False
        e._symcache = {}
        e._order_feeds = {}
        conn = db.get(type(user), user.id)
        good = _FakeClient(close_behaviour=lambda sid, **kw: {"closed": True, "order_id": "ord9"})
        e._conn_for = lambda db_, uid: conn
        e._client = lambda db_, c: good
        # Mock OrderFeed to return filled status immediately for our mock order_id
        class _MockFeed:
            def wait_for_fill(self, order_id, timeout_sec=10.0):
                if order_id == "ord9":
                    return {"status": "complete", "fill_qty": 65, "avg_px": 20.0}
                return None
        e._get_order_feed = lambda db_, uid: _MockFeed()
        e._sell_one(s={"user_id": user.id, "email": user.email}, leg="CE",
                    strike=23800.0, sym={"sym_id": "SCE", "symbol": "NIFTY09JUN23800CE"},
                    action={"premium": 20.0, "reason": "trail_exit"})
    finally:
        ex_mod.session_scope = orig_scope

    db.expire_all()
    r2 = db.get(StraddlePosition, rid)
    assert r2.status == "closed", r2.status
    assert r2.exit_px == 20.0 and r2.exit_order_id == "ord9", (r2.exit_px, r2.exit_order_id)
    print("ok: _sell_one clean close marks row closed (control)")


# ── Fix #7a: reconcile recovers an error-row that actually filled ──
def test_reconcile_recovers_error_row_that_filled():
    if not _try_sqlalchemy():
        print("skip: reconcile error-row (sqlalchemy absent)")
        return
    from app import straddle_executor as ex_mod
    from app import tradejini_auth
    from app.models import StraddlePosition, TradejiniConnection
    from app.straddle_executor import reconcile_on_restart

    db = _session()
    user = _user_with_tj(db)
    today = ex_mod._ist_today()
    # an order that was marked `error` (accept-then-read-failed) but is LIVE at broker
    err_row = StraddlePosition(user_id=user.id, trade_date=today, leg="PE", strike=22800.0,
                               expiry="2026-06-09", sym_id="SPE", symbol="NIFTY09JUN22800PE",
                               qty=65, status="error", detail="buy: read timeout")
    # an `open` row whose broker position is GONE → reconcile closes it
    gone_row = StraddlePosition(user_id=user.id, trade_date=today, leg="CE", strike=23800.0,
                                expiry="2026-06-09", sym_id="SCE", symbol="NIFTY09JUN23800CE",
                                qty=65, status="open")
    db.add_all([err_row, gone_row]); db.commit()
    eid, gid = err_row.id, gone_row.id
    from sqlalchemy import select as _select
    conn = db.execute(_select(TradejiniConnection)).scalars().first()

    orig_scope = ex_mod.session_scope
    orig_live = ex_mod._live_conns
    orig_token = tradejini_auth.ensure_client_token
    orig_client = ex_mod.tradejini.TradejiniClient
    ex_mod.session_scope = _fake_scope(db)
    # `settings` is a frozen dataclass — patch the connection-selector itself rather
    # than the live/dry switches it reads (decouples the test from config internals).
    ex_mod._live_conns = lambda db_: [conn]
    tradejini_auth.ensure_client_token = lambda db_, c: "tok"
    # broker reports ONLY SPE live (SCE gone) → err_row recovers, gone_row closes
    ex_mod.tradejini.TradejiniClient = lambda token, api_key=None: _FakeClient(
        positions=[{"sym_id": "SPE"}])
    try:
        out = reconcile_on_restart()
    finally:
        ex_mod.session_scope = orig_scope
        ex_mod._live_conns = orig_live
        tradejini_auth.ensure_client_token = orig_token
        ex_mod.tradejini.TradejiniClient = orig_client

    db.expire_all()
    assert db.get(StraddlePosition, eid).status == "open", "error-row that filled must recover to open"
    assert "reconciled-error-filled" in (db.get(StraddlePosition, eid).detail or "")
    assert db.get(StraddlePosition, gid).status == "closed", "vanished open-row must close"
    assert out["reconciled"] == 2, out
    print("ok: reconcile recovers error-row-that-filled + closes vanished open-row")


# ── Fix #1: square-off dedups duplicate broker rows for one sym ──
def test_square_off_dedups_duplicate_broker_rows():
    if not _try_sqlalchemy():
        print("skip: square-off dedup (sqlalchemy absent)")
        return
    from app import db as db_mod
    from app import straddle_executor as ex_mod
    from app import straddle_squareoff as so
    from app import telegram_notify, tradejini, tradejini_auth
    from app.config import settings
    from app.models import StraddlePosition, TradejiniConnection

    db = _session()
    user = _user_with_tj(db)
    today = ex_mod._ist_today()
    db.add(StraddlePosition(user_id=user.id, trade_date=today, leg="PE", strike=22800.0,
                            expiry="2026-06-09", sym_id="SPE", symbol="NIFTY09JUN22800PE",
                            qty=65, status="open"))
    db.commit()

    # broker returns the SAME sym_id TWICE (two position rows) — a 2nd close would
    # be a double-sell into a naked short. Dedup ⇒ exactly one close_position call.
    fake = _FakeClient(positions=[
        {"sym_id": "SPE", "symbol": "NIFTY09JUN22800PE", "size": 65},
        {"sym_id": "SPE", "symbol": "NIFTY09JUN22800PE", "size": 65}])

    conns = db.execute(__import__("sqlalchemy").select(TradejiniConnection)).scalars().all()
    orig = {
        "db_scope": db_mod.session_scope,
        "live_conns": ex_mod._live_conns,
        "token": tradejini_auth.ensure_client_token,
        "client": tradejini.TradejiniClient,
        "send": telegram_notify.send_message,
        "lock": so._SQUAREOFF_LOCK, "fp": so._lock_fp,
    }
    import tempfile
    db_mod.session_scope = _fake_scope(db)
    ex_mod._live_conns = lambda db_: conns
    tradejini_auth.ensure_client_token = lambda db_, c: "tok"
    tradejini.TradejiniClient = lambda token, api_key=None: fake
    telegram_notify.send_message = lambda *a, **k: None
    so._SQUAREOFF_LOCK = os.path.join(tempfile.mkdtemp(), ".sq.lock")
    so._lock_fp = None
    try:
        res = so.square_off_all()
    finally:
        if so._lock_fp is not None:
            so._lock_fp.close()
        db_mod.session_scope = orig["db_scope"]
        ex_mod._live_conns = orig["live_conns"]
        tradejini_auth.ensure_client_token = orig["token"]
        tradejini.TradejiniClient = orig["client"]
        telegram_notify.send_message = orig["send"]
        so._SQUAREOFF_LOCK, so._lock_fp = orig["lock"], orig["fp"]

    assert fake.close_calls == [("SPE", "normal", None)], f"expected ONE close, got {fake.close_calls}"
    assert res["closed"] == 1, res
    print("ok: square-off closes one sym once despite duplicate broker rows")


# ── Fix #1: square-off single-instance lock is exclusive ──
def test_square_off_lock_is_exclusive():
    from app import straddle_squareoff as so
    import fcntl
    import tempfile
    lk = os.path.join(tempfile.mkdtemp(), ".sq.lock")
    orig_lock, orig_fp = so._SQUAREOFF_LOCK, so._lock_fp
    so._SQUAREOFF_LOCK = lk
    so._lock_fp = None
    try:
        assert so._acquire_lock() is True            # first holder wins
        fp2 = open(lk, "w")
        try:
            try:
                fcntl.flock(fp2.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                refused = False
            except OSError:
                refused = True
            assert refused, "a second square-off holder must be refused (no double-close)"
        finally:
            fp2.close()
    finally:
        if so._lock_fp is not None:
            so._lock_fp.close()
        so._SQUAREOFF_LOCK, so._lock_fp = orig_lock, orig_fp
    print("ok: square-off single-instance lock is exclusive")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} test(s) passed")
