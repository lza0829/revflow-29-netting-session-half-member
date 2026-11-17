"""测试基础设施：统一测试库路径、每用例重置、多账户对向夹具。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("DATA_DIR", str(ROOT / "backend" / "data_test"))
os.environ.setdefault("FIXTURES_DIR", str(ROOT / "fixtures"))

from app import seed  # noqa: E402
from app.db import connect  # noqa: E402

TABLES = (
    "verify_reports",
    "quarantine_items",
    "netting_session_proof_members",
    "netting_session_proofs",
    "netting_session_entries",
    "netting_session_accounts",
    "netting_sessions",
    "ledger_entries",
    "settle_runs",
    "accounts",
)

ACCOUNT_A = 101
ACCOUNT_B = 102
ACCOUNT_C = 103


def reset_database() -> None:
    """清空所有表并按 fixtures/seed.json 重新装载，保证用例互不污染。"""
    with connect() as conn:
        for table in TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    seed.init_db()


def _seed_netting_fixture() -> None:
    """多账户对向夹具：两个净额相反的清算户 + 一个只作旁证的未参与户。"""
    with connect() as conn:
        for acc_id, code, name in (
            (ACCOUNT_A, "NT-A", "轧差甲户"),
            (ACCOUNT_B, "NT-B", "轧差乙户"),
            (ACCOUNT_C, "NT-C", "旁证丙户"),
        ):
            conn.execute(
                "INSERT INTO accounts(id, code, name, currency, balance_cents) VALUES (?, ?, ?, 'CNY', 0)",
                (acc_id, code, name),
            )
        rows = [
            # 甲户正向 30000，乙户冲正 30000 指向甲户原单（对向）
            (1001, "NT-FWD-A1", ACCOUNT_A, "正向", 30000, None),
            # 乙户正向 22000，甲户冲正 22000 指向乙户原单（对向）
            (1002, "NT-FWD-B1", ACCOUNT_B, "正向", 22000, None),
            (1003, "NT-REV-A1", ACCOUNT_A, "冲正", 22000, "NT-FWD-B1"),
            (1004, "NT-REV-B1", ACCOUNT_B, "冲正", 30000, "NT-FWD-A1"),
            # 旁证户：不参与会话，余额不得变化
            (1005, "NT-FWD-C1", ACCOUNT_C, "正向", 7000, None),
        ]
        for entry_id, key, acc, kind, amount, target in rows:
            conn.execute(
                """
                INSERT INTO ledger_entries(
                  id, entry_key, account_id, kind, amount_cents, biz_ref,
                  target_entry_key, booked_at, status, note
                ) VALUES (?, ?, ?, ?, ?, '', ?, '2026-11-16T08:00:00Z', '待归并', '')
                """,
                (entry_id, key, acc, kind, amount, target),
            )
        conn.commit()


@pytest.fixture(autouse=True)
def fresh_database():
    reset_database()
    _seed_netting_fixture()
    yield
