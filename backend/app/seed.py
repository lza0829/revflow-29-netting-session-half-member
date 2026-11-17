from __future__ import annotations

import json
from pathlib import Path

from app.db import FIXTURES_DIR, connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  currency TEXT NOT NULL DEFAULT 'CNY',
  balance_cents INTEGER NOT NULL DEFAULT 0,
  last_settle_run_id INTEGER
);
CREATE TABLE IF NOT EXISTS ledger_entries (
  id INTEGER PRIMARY KEY,
  entry_key TEXT NOT NULL UNIQUE,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  kind TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  biz_ref TEXT NOT NULL DEFAULT '',
  target_entry_key TEXT,
  booked_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '待归并',
  note TEXT NOT NULL DEFAULT '',
  settle_run_id INTEGER
);
CREATE TABLE IF NOT EXISTS quarantine_items (
  id INTEGER PRIMARY KEY,
  entry_key TEXT NOT NULL,
  account_id INTEGER NOT NULL,
  reason TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  settle_run_id INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settle_runs (
  id INTEGER PRIMARY KEY,
  status TEXT NOT NULL,
  forward_count INTEGER NOT NULL DEFAULT 0,
  reversal_count INTEGER NOT NULL DEFAULT 0,
  settled_count INTEGER NOT NULL DEFAULT 0,
  quarantine_count INTEGER NOT NULL DEFAULT 0,
  net_cents INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS verify_reports (
  id INTEGER PRIMARY KEY,
  settle_run_id INTEGER REFERENCES settle_runs(id),
  ledger_net_cents INTEGER NOT NULL,
  balance_sum_cents INTEGER NOT NULL,
  quarantine_count INTEGER NOT NULL,
  consistent INTEGER NOT NULL,
  detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS netting_sessions (
  id INTEGER PRIMARY KEY,
  status TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  failure_reason TEXT,
  account_count INTEGER NOT NULL DEFAULT 0,
  entry_count INTEGER NOT NULL DEFAULT 0,
  net_total_cents INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS netting_session_accounts (
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES netting_sessions(id),
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  account_code TEXT NOT NULL,
  account_name TEXT NOT NULL,
  before_balance_cents INTEGER NOT NULL,
  net_cents INTEGER NOT NULL,
  after_balance_cents INTEGER NOT NULL,
  applied INTEGER NOT NULL DEFAULT 0,
  UNIQUE(session_id, account_id)
);
CREATE TABLE IF NOT EXISTS netting_session_entries (
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES netting_sessions(id),
  entry_id INTEGER NOT NULL REFERENCES ledger_entries(id),
  entry_key TEXT NOT NULL,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  kind TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  target_entry_key TEXT,
  signed_cents INTEGER NOT NULL,
  UNIQUE(session_id, entry_id)
);
-- 会话证明：会话落库同事务物化，成员账户 / 各户净额 / 会话合计 / 成败的不可变凭证
CREATE TABLE IF NOT EXISTS netting_session_proofs (
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL UNIQUE REFERENCES netting_sessions(id),
  outcome TEXT NOT NULL,
  member_count INTEGER NOT NULL DEFAULT 0,
  entry_count INTEGER NOT NULL DEFAULT 0,
  net_total_cents INTEGER NOT NULL DEFAULT 0,
  before_sum_cents INTEGER NOT NULL DEFAULT 0,
  after_sum_cents INTEGER NOT NULL DEFAULT 0,
  applied_count INTEGER NOT NULL DEFAULT 0,
  failure_reason TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS netting_session_proof_members (
  id INTEGER PRIMARY KEY,
  proof_id INTEGER NOT NULL REFERENCES netting_session_proofs(id),
  session_id INTEGER NOT NULL REFERENCES netting_sessions(id),
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  account_code TEXT NOT NULL,
  before_balance_cents INTEGER NOT NULL,
  net_cents INTEGER NOT NULL,
  after_balance_cents INTEGER NOT NULL,
  applied INTEGER NOT NULL DEFAULT 0,
  UNIQUE(proof_id, account_id)
);
"""

# 旧库补列：底座只有 settle_runs，本轮新增轧差会话指针列
MIGRATIONS = (
    "ALTER TABLE ledger_entries ADD COLUMN netting_session_id INTEGER",
    "ALTER TABLE accounts ADD COLUMN last_netting_session_id INTEGER",
)


def _migrate(conn) -> None:
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except Exception as exc:  # sqlite3.OperationalError: duplicate column
            if "duplicate column" not in str(exc).lower():
                raise


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        n = conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"]
        if n:
            return
        seed_path = FIXTURES_DIR / "seed.json"
        data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
        for a in data["accounts"]:
            conn.execute(
                "INSERT INTO accounts(id, code, name, currency, balance_cents) VALUES (?, ?, ?, ?, 0)",
                (a["id"], a["code"], a["name"], a["currency"]),
            )
        for e in data["forward_entries"]:
            conn.execute(
                """
                INSERT INTO ledger_entries(
                  id, entry_key, account_id, kind, amount_cents, biz_ref,
                  target_entry_key, booked_at, status, note
                ) VALUES (?, ?, ?, '正向', ?, ?, NULL, ?, '待归并', ?)
                """,
                (
                    e["id"],
                    e["entry_key"],
                    e["account_id"],
                    e["amount_cents"],
                    e.get("biz_ref", ""),
                    e["booked_at"],
                    e.get("note", ""),
                ),
            )
        # 冲正 id 与正向可能重叠，用独立自增：先取 max
        max_id = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM ledger_entries").fetchone()["m"]
        for i, e in enumerate(data["reversal_entries"], start=1):
            conn.execute(
                """
                INSERT INTO ledger_entries(
                  id, entry_key, account_id, kind, amount_cents, biz_ref,
                  target_entry_key, booked_at, status, note
                ) VALUES (?, ?, ?, '冲正', ?, ?, ?, ?, '待归并', ?)
                """,
                (
                    max_id + i,
                    e["entry_key"],
                    e["account_id"],
                    e["amount_cents"],
                    e.get("biz_ref", ""),
                    e.get("target_entry_key"),
                    e["booked_at"],
                    e.get("note", ""),
                ),
            )
        conn.commit()
