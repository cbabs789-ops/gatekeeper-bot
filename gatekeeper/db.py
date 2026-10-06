"""SQLite storage. One file, no server to run."""
import sqlite3
from .config import DB_PATH, DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS coins (
  mint TEXT PRIMARY KEY,
  symbol TEXT, name TEXT,
  graduated_at INTEGER,          -- ms
  grad_price REAL,               -- first price seen after graduation
  pair TEXT, dex TEXT,
  last_seen INTEGER,
  status TEXT DEFAULT 'watching' -- watching | dead | expired
);
CREATE TABLE IF NOT EXISTS snapshots (
  mint TEXT, ts INTEGER,
  price REAL, liq REAL, fdv REAL,
  vol_m5 REAL, vol_h1 REAL,
  buys_m5 INTEGER, sells_m5 INTEGER, buys_h1 INTEGER, sells_h1 INTEGER,
  pc_m5 REAL, pc_h1 REAL
);
CREATE INDEX IF NOT EXISTS snap_ts ON snapshots(ts);
CREATE INDEX IF NOT EXISTS snap_mint_ts ON snapshots(mint, ts);
CREATE TABLE IF NOT EXISTS safety (
  mint TEXT PRIMARY KEY, checked_at INTEGER,
  mint_revoked INTEGER, freeze_revoked INTEGER,
  lp_locked REAL, top10 REAL, insiders INTEGER, danger TEXT, rc_score REAL
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mode TEXT, run_id TEXT,
  mint TEXT, symbol TEXT,
  opened_at INTEGER, entry_price REAL, size_usd REAL, qty REAL,
  closed_at INTEGER, proceeds_usd REAL, pnl_usd REAL, pnl_pct REAL,
  exit_reason TEXT, why_entered TEXT, legs TEXT
);
CREATE TABLE IF NOT EXISTS launches (day TEXT PRIMARY KEY, created INTEGER DEFAULT 0, graduated INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


def connect(path=None):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path or DB_PATH), timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    con.executescript(SAFETY_LOG)
    for sql in ("ALTER TABLE coins ADD COLUMN chain TEXT DEFAULT 'solana'",
                "ALTER TABLE coins ADD COLUMN added_at INTEGER",
                "ALTER TABLE coins ADD COLUMN source TEXT",
                "ALTER TABLE safety ADD COLUMN lp_na INTEGER DEFAULT 0",
                "ALTER TABLE safety ADD COLUMN creator TEXT",
                "ALTER TABLE safety ADD COLUMN creator_prev INTEGER",
                "ALTER TABLE safety ADD COLUMN creator_dead INTEGER",
                "ALTER TABLE coins ADD COLUMN socials TEXT"):
        try:
            con.execute(sql)
        except sqlite3.OperationalError:
            pass  # already there
    return con


SAFETY_LOG = """
CREATE TABLE IF NOT EXISTS safety_log (
  mint TEXT, checked_at INTEGER, mint_revoked INTEGER, freeze_revoked INTEGER, lp_locked REAL, top10 REAL,
  insiders INTEGER, danger TEXT, lp_na INTEGER, creator_prev INTEGER, creator_dead INTEGER
);
CREATE INDEX IF NOT EXISTS safety_log_mint ON safety_log(mint, checked_at);
"""


def save_safety(con, res):
    # keep every check, not just the latest: the rule test must see what was known AT THE TIME, not what we know now
    try:
        row = dict({"lp_na": 0, "insiders": None, "top10": None, "danger": None, "creator_prev": None, "creator_dead": None,
                    "lp_locked": None, "mint_revoked": None, "freeze_revoked": None}, **res)
        con.execute("INSERT INTO safety_log VALUES(:mint,:checked_at,:mint_revoked,:freeze_revoked,:lp_locked,:top10,:insiders,:danger,"
                    ":lp_na,:creator_prev,:creator_dead)", row)
    except Exception:  # noqa: BLE001
        pass
    con.execute("INSERT OR REPLACE INTO safety(mint, checked_at, mint_revoked, freeze_revoked, lp_locked, top10, insiders, danger, rc_score, lp_na, "
                "creator, creator_prev, creator_dead) VALUES(:mint,:checked_at,:mint_revoked,:freeze_revoked,:lp_locked,:top10,:insiders,:danger,"
                ":rc_score,:lp_na,:creator,:creator_prev,:creator_dead)",
                dict({"lp_na": 0, "rc_score": None, "insiders": None, "top10": None, "danger": None,
                      "creator": None, "creator_prev": None, "creator_dead": None}, **res))


def kv_get(con, k, default=None):
    r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def kv_set(con, k, v):
    con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
