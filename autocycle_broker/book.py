"""
autocycle_broker/book.py
────────────────────────
Internal order book — all positions are tracked at mid prices.
SQLite gives persistence across restarts.

P&L formula:
  price_change × lot × CONTRACT_SIZE = dollar P&L

  XAUUSD+ (CONTRACT_SIZE=100):  $1 move × 0.01 lot × 100 = $1.00
  BTCUSD  (CONTRACT_SIZE=1):    $1 move × 0.01 lot × 1   = $0.01
"""
import sqlite3
import time
from contextlib import contextmanager

from . import config


# ─── DB helpers ──────────────────────────────────────────────────────────────

@contextmanager
def _db():
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create tables if they don't exist (safe to call on every startup)."""
    with _db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS broker_state (
                key   TEXT PRIMARY KEY,
                value REAL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id      INTEGER NOT NULL,
                side          TEXT NOT NULL,       -- 'BUY' or 'SELL'
                entry         REAL NOT NULL,       -- mid price at open
                sl            REAL NOT NULL,       -- SL level (mid)
                tp            REAL NOT NULL,       -- full TP level (beyond SL)
                lot           REAL NOT NULL,
                contract_size REAL NOT NULL DEFAULT 100,  -- MT5 contract size at open
                status        TEXT NOT NULL DEFAULT 'OPEN',
                open_ts       REAL NOT NULL,
                close_ts      REAL,
                close_price   REAL,
                pnl           REAL,
                close_reason  TEXT               -- 'SL','TP','REVERSAL','GUARDIAN','MANUAL'
            )
        """)
        # Migration: add contract_size to existing databases
        try:
            db.execute("ALTER TABLE positions ADD COLUMN contract_size REAL NOT NULL DEFAULT 100")
        except Exception:
            pass  # column already exists
        db.execute("""
            CREATE TABLE IF NOT EXISTS cycles (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                open_ts     REAL NOT NULL,
                close_ts    REAL,
                symbol      TEXT DEFAULT 'XAUUSD+',
                lot         REAL,
                atr         REAL,
                sl_dist     REAL,
                tp_extra    REAL,
                mid_entry   REAL,
                outcome     TEXT,              -- 'BREAKEVEN','TP','REVERSAL_AFTER_HEDGE'
                gross_pnl   REAL,
                net_pnl     REAL,
                vantage_pnl REAL DEFAULT 0
            )
        """)
        # Migration: add symbol column to existing databases
        try:
            db.execute("ALTER TABLE cycles ADD COLUMN symbol TEXT DEFAULT 'XAUUSD+'")
        except Exception:
            pass  # column already exists
        db.execute("""
            CREATE TABLE IF NOT EXISTS hedge_positions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id       INTEGER NOT NULL,
                vantage_ticket INTEGER,
                side           TEXT,
                lot            REAL,
                open_ts        REAL,
                open_price     REAL,
                close_ts       REAL,
                close_price    REAL,
                pnl            REAL,
                status         TEXT NOT NULL DEFAULT 'OPEN'
            )
        """)
        # Seed starting balance if this is a fresh database
        db.execute(
            "INSERT OR IGNORE INTO broker_state (key, value) VALUES ('balance', 10.0)"
        )


# ─── Balance ─────────────────────────────────────────────────────────────────

def get_balance() -> float:
    with _db() as db:
        row = db.execute(
            "SELECT value FROM broker_state WHERE key='balance'"
        ).fetchone()
        return round(float(row['value']), 2) if row else 500.0


def set_balance(amount: float):
    with _db() as db:
        db.execute(
            "INSERT OR REPLACE INTO broker_state (key, value) VALUES ('balance', ?)",
            (round(amount, 2),)
        )


def adjust_balance(delta: float):
    set_balance(get_balance() + delta)


# ─── Lot sizing ───────────────────────────────────────────────────────────────

def lot_for_balance() -> float:
    # If FIXED_LOT is set in .env, always use that regardless of balance.
    # This is useful when your Vantage account is small but internal balance
    # is large — prevents lot size from being pushed up unexpectedly.
    if config.FIXED_LOT > 0:
        return config.FIXED_LOT
    bal = get_balance()
    lot = 0.01
    for tier in config.LOT_TIERS:
        if bal >= tier['min']:
            lot = tier['lot']
    return lot


def lot_for_risk(sl_dist: float, contract_size: float, risk_pct: float = 0.01) -> float:
    """
    Risk-based lot sizing: size the trade so a full SL loss = risk_pct of balance.

    Formula:  lot = (balance × risk_pct) / (sl_dist × contract_size)

    Examples (Gold, contract_size=100):
      balance=$200, sl=$1.50 → lot = (200×0.01)/(1.50×100) = 0.01
      balance=$500, sl=$1.50 → lot = (500×0.01)/(1.50×100) = 0.03
      balance=$1000,sl=$1.50 → lot = (1000×0.01)/(1.50×100) = 0.07

    Clamped: min 0.01, max 1.0. Rounded to nearest 0.01.
    FIXED_LOT in .env always wins.
    """
    if config.FIXED_LOT > 0:
        return config.FIXED_LOT

    bal = get_balance()

    if sl_dist <= 0 or contract_size <= 0:
        return lot_for_balance()   # fallback

    risk_dollars = bal * risk_pct
    raw_lot      = risk_dollars / (sl_dist * contract_size)

    # Round to nearest 0.01, clamp to [0.01, 1.0]
    lot = round(max(0.01, min(1.0, raw_lot)) / 0.01) * 0.01
    return round(lot, 2)


# ─── Cycle lifecycle ──────────────────────────────────────────────────────────

def open_cycle(lot: float, atr: float, sl_dist: float, tp_extra: float,
               mid: float, symbol: str = 'XAUUSD+',
               contract_size: float = 100.0) -> int:
    """
    Create a new cycle + its matching BUY and SELL internal positions.
    Returns the cycle_id.

    Layout (mid = 3350.00, sl_dist = 2.00, tp_extra = 0.40):
      BUY  entry=3350  sl=3348  tp=3352.40   (profits going UP)
      SELL entry=3350  sl=3352  tp=3347.60   (profits going DOWN)

    contract_size: MT5 trade_contract_size (100 for XAUUSD+, 1 for BTCUSD)
    """
    sl_dist  = round(sl_dist,  2)
    tp_extra = round(tp_extra, 2)
    mid      = round(mid,      2)
    ts       = time.time()

    with _db() as db:
        cur = db.execute(
            """INSERT INTO cycles (open_ts, symbol, lot, atr, sl_dist, tp_extra, mid_entry)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ts, symbol, lot, round(atr, 2), sl_dist, tp_extra, mid)
        )
        cycle_id = cur.lastrowid

        # BUY leg
        db.execute(
            """INSERT INTO positions
               (cycle_id, side, entry, sl, tp, lot, contract_size, open_ts)
               VALUES (?, 'BUY', ?, ?, ?, ?, ?, ?)""",
            (cycle_id, mid,
             round(mid - sl_dist, 2),              # BUY SL = below entry
             round(mid + sl_dist + tp_extra, 2),   # BUY TP = above entry + extension
             lot, contract_size, ts)
        )
        # SELL leg
        db.execute(
            """INSERT INTO positions
               (cycle_id, side, entry, sl, tp, lot, contract_size, open_ts)
               VALUES (?, 'SELL', ?, ?, ?, ?, ?, ?)""",
            (cycle_id, mid,
             round(mid + sl_dist, 2),              # SELL SL = above entry
             round(mid - sl_dist - tp_extra, 2),   # SELL TP = below entry - extension
             lot, contract_size, ts)
        )
    return cycle_id


def get_open_positions(cycle_id: int) -> list[dict]:
    with _db() as db:
        rows = db.execute(
            "SELECT * FROM positions WHERE cycle_id=? AND status='OPEN'",
            (cycle_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_positions(cycle_id: int) -> list[dict]:
    with _db() as db:
        rows = db.execute(
            "SELECT * FROM positions WHERE cycle_id=? ORDER BY id",
            (cycle_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def close_position(pos_id: int, close_price: float, reason: str) -> float:
    """
    Close an internal position at close_price (mid).
    Calculates P&L based on side, records it, returns the P&L.
    Does NOT touch balance — balance is updated when the cycle closes.
    """
    with _db() as db:
        row = db.execute(
            "SELECT * FROM positions WHERE id=?", (pos_id,)
        ).fetchone()
        if not row:
            return 0.0
        r = dict(row)

        # P&L = price_change × lot × contract_size
        # contract_size is stored per-position at open time (correct for mixed-symbol history)
        cs = r.get('contract_size') or config.CONTRACT_SIZE
        if r['side'] == 'BUY':
            pnl = round((close_price - r['entry']) * r['lot'] * cs, 2)
        else:
            pnl = round((r['entry'] - close_price) * r['lot'] * cs, 2)

        db.execute(
            """UPDATE positions
               SET status='CLOSED', close_ts=?, close_price=?, pnl=?, close_reason=?
               WHERE id=?""",
            (time.time(), round(close_price, 2), pnl, reason, pos_id)
        )
    return pnl


def close_cycle(cycle_id: int, outcome: str,
                gross_pnl: float, vantage_pnl: float = 0.0):
    """
    Finalise a cycle: record outcome, apply commission, update internal balance.
    net_pnl = gross_pnl − COMMISSION
    vantage_pnl is the real-money P&L from the Vantage hedge (informational).
    """
    net_pnl = round(gross_pnl - config.COMMISSION, 2)
    with _db() as db:
        db.execute(
            """UPDATE cycles
               SET close_ts=?, outcome=?, gross_pnl=?, net_pnl=?, vantage_pnl=?
               WHERE id=?""",
            (time.time(), outcome,
             round(gross_pnl, 2), net_pnl, round(vantage_pnl, 2),
             cycle_id)
        )
    adjust_balance(net_pnl)


# ─── Hedge positions (Vantage) ────────────────────────────────────────────────

def open_hedge(cycle_id: int, vantage_ticket: int,
               side: str, lot: float, open_price: float):
    with _db() as db:
        db.execute(
            """INSERT INTO hedge_positions
               (cycle_id, vantage_ticket, side, lot, open_ts, open_price)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (cycle_id, vantage_ticket, side, lot, time.time(), round(open_price, 2))
        )


def close_hedge(cycle_id: int, close_price: float) -> float:
    """Record hedge close, return the Vantage-side P&L (for informational logging)."""
    with _db() as db:
        row = db.execute(
            "SELECT * FROM hedge_positions WHERE cycle_id=? AND status='OPEN'",
            (cycle_id,)
        ).fetchone()
        if not row:
            return 0.0
        r = dict(row)

        # Read contract_size from the first position of this cycle (stored at open time)
        pos = db.execute(
            "SELECT contract_size FROM positions WHERE cycle_id=? LIMIT 1", (cycle_id,)
        ).fetchone()
        cs = float(pos['contract_size']) if pos else config.CONTRACT_SIZE

        if r['side'] == 'SELL':
            pnl = round((r['open_price'] - close_price) * r['lot'] * cs, 2)
        else:
            pnl = round((close_price - r['open_price']) * r['lot'] * cs, 2)

        db.execute(
            """UPDATE hedge_positions
               SET status='CLOSED', close_ts=?, close_price=?, pnl=?
               WHERE cycle_id=? AND status='OPEN'""",
            (time.time(), round(close_price, 2), pnl, cycle_id)
        )
    return pnl


def get_open_hedge(cycle_id: int) -> dict | None:
    with _db() as db:
        row = db.execute(
            "SELECT * FROM hedge_positions WHERE cycle_id=? AND status='OPEN'",
            (cycle_id,)
        ).fetchone()
        return dict(row) if row else None


# ─── History and state ────────────────────────────────────────────────────────

def get_history(limit: int = 50) -> list[dict]:
    with _db() as db:
        rows = db.execute(
            "SELECT * FROM cycles ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_current_cycle() -> dict | None:
    """Return the most recent unclosed cycle, or None."""
    with _db() as db:
        row = db.execute(
            "SELECT * FROM cycles WHERE close_ts IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_cycle(cycle_id: int) -> dict | None:
    with _db() as db:
        row = db.execute(
            "SELECT * FROM cycles WHERE id=?", (cycle_id,)
        ).fetchone()
        return dict(row) if row else None
