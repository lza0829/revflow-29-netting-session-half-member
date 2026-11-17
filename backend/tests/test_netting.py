from pathlib import Path
import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]

from app.db import connect  # noqa: E402
from app.main import app  # noqa: E402

# 与 conftest.py 的多账户对向夹具保持一致
ACCOUNT_A = 101
ACCOUNT_B = 102
ACCOUNT_C = 103


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _balances(client) -> dict[int, int]:
    return {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}


def test_netting_session_offsets_opposing_reversals(client):
    before = _balances(client)

    resp = client.post(
        "/api/netting-sessions",
        json={
            "account_ids": [ACCOUNT_A, ACCOUNT_B],
            "entry_ids": [1001, 1002, 1003, 1004],
            "note": "联营对向轧差",
        },
    )
    assert resp.status_code == 200, resp.text
    session = resp.json()
    assert session["status"] == "完成", session
    assert session["account_count"] == 2
    assert session["entry_count"] == 4
    # 甲：+30000 −22000 = +8000；乙：+22000 −30000 = −8000；向量合计为 0
    assert session["net_total_cents"] == 0
    vector = {a["account_id"]: a["net_cents"] for a in session["accounts"]}
    assert vector == {ACCOUNT_A: 8000, ACCOUNT_B: -8000}

    after = _balances(client)
    assert after[ACCOUNT_A] == before[ACCOUNT_A] + 8000
    assert after[ACCOUNT_B] == before[ACCOUNT_B] - 8000
    assert after[ACCOUNT_C] == before[ACCOUNT_C]

    # 成员流水一次全部标已归并，并挂会话外键
    entries = client.get("/api/entries").json()
    member = {e["id"]: e for e in entries if e["id"] in (1001, 1002, 1003, 1004)}
    assert all(e["status"] == "已归并" for e in member.values())
    assert all(e["netting_session_id"] == session["id"] for e in member.values())
    bystander = next(e for e in entries if e["id"] == 1005)
    assert bystander["status"] == "待归并"
    assert bystander["netting_session_id"] is None

    # 刷新（重新查询）后会话仍在
    again = client.get(f"/api/netting-sessions/{session['id']}").json()
    assert again["status"] == "完成"
    assert len(again["accounts"]) == 2
    assert len(again["entries"]) == 4

    report = client.get(f"/api/netting-sessions/{session['id']}/report").json()
    rows = {a["account_id"]: a for a in report["accounts"]}
    assert rows[ACCOUNT_A]["before_balance_cents"] == before[ACCOUNT_A]
    assert rows[ACCOUNT_A]["after_balance_cents"] == before[ACCOUNT_A] + 8000
    assert rows[ACCOUNT_A]["net_cents"] == 8000
    assert rows[ACCOUNT_B]["before_balance_cents"] == before[ACCOUNT_B]
    assert rows[ACCOUNT_B]["after_balance_cents"] == before[ACCOUNT_B] - 8000
    assert report["totals"]["session_net_cents"] == 0
    assert report["totals"]["member_net_cents"] == 0
    assert report["totals"]["entry_signed_cents"] == 0
    assert report["totals"]["ledger_signed_cents"] == 0
    assert all(report["checks"].values()), report["checks"]


def test_netting_session_failure_is_all_or_nothing(client):
    with connect() as conn:
        # 新一组：甲户正向，乙户一条无原单冲正 → 整会话必须失败
        conn.execute(
            "INSERT INTO ledger_entries(id, entry_key, account_id, kind, amount_cents, biz_ref, target_entry_key, booked_at, status, note)"
            " VALUES (1011, 'NT-FWD-A2', ?, '正向', 5000, '', NULL, '2026-11-16T09:00:00Z', '待归并', '')",
            (ACCOUNT_A,),
        )
        conn.execute(
            "INSERT INTO ledger_entries(id, entry_key, account_id, kind, amount_cents, biz_ref, target_entry_key, booked_at, status, note)"
            " VALUES (1012, 'NT-REV-B2', ?, '冲正', 5000, '', 'NT-MISSING', '2026-11-16T09:10:00Z', '待归并', '')",
            (ACCOUNT_B,),
        )
        conn.commit()

    before = _balances(client)
    resp = client.post(
        "/api/netting-sessions",
        json={"account_ids": [ACCOUNT_A, ACCOUNT_B], "entry_ids": [1011, 1012]},
    )
    assert resp.status_code == 200, resp.text
    session = resp.json()
    assert session["status"] == "失败"
    assert "无原单" in (session["failure_reason"] or "")

    after = _balances(client)
    assert after[ACCOUNT_A] == before[ACCOUNT_A]
    assert after[ACCOUNT_B] == before[ACCOUNT_B]

    entries = {e["id"]: e for e in client.get("/api/entries").json()}
    assert entries[1011]["status"] == "待归并"
    assert entries[1012]["status"] == "待归并"
    assert entries[1011]["netting_session_id"] is None

    report = client.get(f"/api/netting-sessions/{session['id']}/report").json()
    assert report["totals"]["applied_count"] == 0
    assert report["checks"]["applied_all_or_none"] is True
    # 失败会话报告仍给出每户会话前余额与草稿净额，便于对账
    assert {a["account_id"] for a in report["accounts"]} == {ACCOUNT_A, ACCOUNT_B}


def test_netting_session_requires_two_accounts(client):
    session = client.post(
        "/api/netting-sessions",
        json={"account_ids": [ACCOUNT_A], "entry_ids": [1001]},
    ).json()
    assert session["status"] == "失败"
    assert "两个参与账户" in session["failure_reason"]


def test_netting_session_rejects_committed_entries(client):
    # 已归并流水不可再入会话；用独立流水，避免依赖其他用例的执行次序
    with connect() as conn:
        conn.execute(
            "INSERT INTO ledger_entries(id, entry_key, account_id, kind, amount_cents, biz_ref, target_entry_key, booked_at, status, note)"
            " VALUES (1101, 'NT-FWD-A9', ?, '正向', 2000, '', NULL, '2026-11-16T09:30:00Z', '已归并', '')",
            (ACCOUNT_A,),
        )
        conn.commit()

    session = client.post(
        "/api/netting-sessions",
        json={"account_ids": [ACCOUNT_A, ACCOUNT_B], "entry_ids": [1101]},
    ).json()
    assert session["status"] == "失败"
    assert "不可入会话" in session["failure_reason"]


def test_netting_session_excess_reversal_fails_session(client):
    with connect() as conn:
        conn.execute(
            "INSERT INTO ledger_entries(id, entry_key, account_id, kind, amount_cents, biz_ref, target_entry_key, booked_at, status, note)"
            " VALUES (1021, 'NT-FWD-A3', ?, '正向', 1000, '', NULL, '2026-11-16T10:00:00Z', '待归并', '')",
            (ACCOUNT_A,),
        )
        conn.execute(
            "INSERT INTO ledger_entries(id, entry_key, account_id, kind, amount_cents, biz_ref, target_entry_key, booked_at, status, note)"
            " VALUES (1022, 'NT-REV-B3', ?, '冲正', 1500, '', 'NT-FWD-A3', '2026-11-16T10:10:00Z', '待归并', '')",
            (ACCOUNT_B,),
        )
        conn.commit()

    before = _balances(client)
    session = client.post(
        "/api/netting-sessions",
        json={"account_ids": [ACCOUNT_A, ACCOUNT_B], "entry_ids": [1021, 1022]},
    ).json()
    assert session["status"] == "失败"
    assert "超额" in session["failure_reason"]
    assert _balances(client) == before
    entries = {e["id"]: e for e in client.get("/api/entries").json()}
    assert entries[1021]["status"] == "待归并"
    assert entries[1022]["status"] == "待归并"


def test_netting_preview_is_read_only(client):
    with connect() as conn:
        conn.execute(
            "INSERT INTO ledger_entries(id, entry_key, account_id, kind, amount_cents, biz_ref, target_entry_key, booked_at, status, note)"
            " VALUES (1041, 'NT-FWD-A5', ?, '正向', 1000, '', NULL, '2026-11-16T12:00:00Z', '待归并', '')",
            (ACCOUNT_A,),
        )
        conn.execute(
            "INSERT INTO ledger_entries(id, entry_key, account_id, kind, amount_cents, biz_ref, target_entry_key, booked_at, status, note)"
            " VALUES (1042, 'NT-REV-B5', ?, '冲正', 1500, '', 'NT-FWD-A5', '2026-11-16T12:10:00Z', '待归并', '')",
            (ACCOUNT_B,),
        )
        conn.commit()

    before = _balances(client)
    preview = client.post(
        "/api/netting-preview",
        json={"account_ids": [ACCOUNT_A, ACCOUNT_B], "entry_ids": [1041, 1042]},
    ).json()
    assert preview["ok"] is False
    assert any("超额" in r for r in preview["reasons"])
    # 预览只读：余额不变、不物化会话
    assert _balances(client) == before
    assert client.get("/api/netting-sessions").json() == []


def test_single_account_settle_is_incremental_on_top_of_netting(client):
    """轧差会话落定后，单账户归并只能增量落账，不得覆盖会话已落余额。"""
    with connect() as conn:
        for acc_id, code in ((201, "NT-D"), (202, "NT-E")):
            conn.execute(
                "INSERT INTO accounts(id, code, name, currency, balance_cents) VALUES (?, ?, ?, 'CNY', 0)",
                (acc_id, code, code),
            )
        rows = [
            (2001, "NT-FWD-D1", 201, "正向", 5000, None),
            (2002, "NT-FWD-E1", 202, "正向", 4000, None),
            (2003, "NT-REV-D1", 201, "冲正", 4000, "NT-FWD-E1"),
            (2004, "NT-REV-E1", 202, "冲正", 5000, "NT-FWD-D1"),
        ]
        for entry_id, key, acc, kind, amount, target in rows:
            conn.execute(
                """
                INSERT INTO ledger_entries(
                  id, entry_key, account_id, kind, amount_cents, biz_ref,
                  target_entry_key, booked_at, status, note
                ) VALUES (?, ?, ?, ?, ?, '', ?, '2026-11-16T11:00:00Z', '待归并', '')
                """,
                (entry_id, key, acc, kind, amount, target),
            )
        conn.commit()

    session = client.post(
        "/api/netting-sessions",
        json={"account_ids": [201, 202], "entry_ids": [2001, 2002, 2003, 2004]},
    ).json()
    assert session["status"] == "完成", session
    before = _balances(client)
    assert before[201] == 1000
    assert before[202] == -1000

    # 会话后再来一笔纯正向流水，跑单账户归并
    with connect() as conn:
        conn.execute(
            "INSERT INTO ledger_entries(id, entry_key, account_id, kind, amount_cents, biz_ref, target_entry_key, booked_at, status, note)"
            " VALUES (2005, 'NT-FWD-D2', 201, '正向', 3000, '', NULL, '2026-11-16T11:30:00Z', '待归并', '')"
        )
        conn.commit()

    run = client.post("/api/settle-runs").json()
    assert run["status"] == "完成"

    after = _balances(client)
    # 会话落定的 1000 仍在，叠加本轮 3000
    assert after[201] == before[201] + 3000
    assert after[202] == before[202]

    report = client.post("/api/verify-reports").json()
    assert report["consistent"] == 1
    assert report["ledger_net_cents"] == report["balance_sum_cents"]


def test_mid_flight_conflict_rolls_back_all_members(client, monkeypatch):
    """落库途中任一成员失败：整会话失败闭，正式余额不留半成员污染，不得假完成。"""
    from app.modules import multi_account_net as svc

    real_build_plan = svc._build_plan

    def tampered_plan(conn, account_ids, entry_ids):
        plan, reasons = real_build_plan(conn, account_ids, entry_ids)
        if not reasons and len(plan["accounts"]) >= 2:
            # 模拟并发：第二名成员的正式余额在事务复核前被外部改写
            victim = plan["accounts"][1]
            victim["before_balance_cents"] += 1
            victim["after_balance_cents"] += 1
        return plan, reasons

    monkeypatch.setattr(svc, "_build_plan", tampered_plan)

    before = _balances(client)
    assert client.get("/api/netting-sessions").json() == []

    resp = client.post(
        "/api/netting-sessions",
        json={
            "account_ids": [ACCOUNT_A, ACCOUNT_B],
            "entry_ids": [1001, 1002, 1003, 1004],
        },
    )
    assert resp.status_code == 200, resp.text
    session = resp.json()
    # 不得假完成：整会话失败闭
    assert session["status"] == "失败"
    assert "余额已变动" in (session["failure_reason"] or "")

    # 正式余额全部保持会话前——包括事务内先更新的第一名成员
    assert _balances(client) == before
    entries = {e["id"]: e for e in client.get("/api/entries").json()}
    for eid in (1001, 1002, 1003, 1004):
        assert entries[eid]["status"] == "待归并"
        assert entries[eid]["netting_session_id"] is None

    # 只物化一条失败会话：不留草稿、不假完成
    sessions = client.get("/api/netting-sessions").json()
    assert len(sessions) == 1
    assert sessions[0]["status"] == "失败"

    # 成员快照刷新后与落库一致：无任何成员落账
    report = client.get(f"/api/netting-sessions/{session['id']}/report").json()
    assert report["totals"]["applied_count"] == 0
    assert report["checks"]["applied_all_or_none"] is True
    assert report["checks"]["failed_no_members_applied"] is True
    assert all(a["applied"] == 0 for a in report["accounts"])

    # 失败证明同源物化
    proof = client.get(f"/api/netting-sessions/{session['id']}/proof").json()
    assert proof["outcome"] == "失败"
    assert proof["applied_count"] == 0
    assert proof["member_count"] == 2
    assert "余额已变动" in proof["failure_reason"]
    assert all(m["applied"] == 0 for m in proof["members"])


def test_netting_session_proof_materialized_for_completed_session(client):
    """完成会话：证明与会话、成员、余额同源，可按会话 id 只读查询。"""
    before = _balances(client)
    session = client.post(
        "/api/netting-sessions",
        json={
            "account_ids": [ACCOUNT_A, ACCOUNT_B],
            "entry_ids": [1001, 1002, 1003, 1004],
        },
    ).json()
    assert session["status"] == "完成"

    resp = client.get(f"/api/netting-sessions/{session['id']}/proof")
    assert resp.status_code == 200, resp.text
    proof = resp.json()
    assert proof["session_id"] == session["id"]
    assert proof["outcome"] == "完成"
    assert proof["member_count"] == 2
    assert proof["entry_count"] == 4
    assert proof["net_total_cents"] == 0
    assert proof["applied_count"] == 2
    assert proof["failure_reason"] is None

    vector = {m["account_id"]: m["net_cents"] for m in proof["members"]}
    assert vector == {ACCOUNT_A: 8000, ACCOUNT_B: -8000}
    members = {m["account_id"]: m for m in proof["members"]}
    assert members[ACCOUNT_A]["before_balance_cents"] == before[ACCOUNT_A]
    assert members[ACCOUNT_A]["after_balance_cents"] == before[ACCOUNT_A] + 8000
    assert members[ACCOUNT_B]["after_balance_cents"] == before[ACCOUNT_B] - 8000
    assert all(m["applied"] == 1 for m in proof["members"])

    # 证明合计与会话报告（物化行 SQL 复算）同源一致
    report = client.get(f"/api/netting-sessions/{session['id']}/report").json()
    assert proof["net_total_cents"] == report["totals"]["member_net_cents"]
    assert proof["before_sum_cents"] == report["totals"]["before_sum_cents"]
    assert proof["after_sum_cents"] == report["totals"]["after_sum_cents"]
    assert proof["applied_count"] == report["totals"]["applied_count"]
    assert report["checks"]["proof_recorded"] is True
    assert report["checks"]["proof_matches_members"] is True


def test_netting_session_proof_records_failure_closed(client):
    """失败会话：证明记录成败=失败且零成员落账，正式余额保持会话前。"""
    with connect() as conn:
        conn.execute(
            "INSERT INTO ledger_entries(id, entry_key, account_id, kind, amount_cents, biz_ref, target_entry_key, booked_at, status, note)"
            " VALUES (1011, 'NT-FWD-A2', ?, '正向', 5000, '', NULL, '2026-11-16T09:00:00Z', '待归并', '')",
            (ACCOUNT_A,),
        )
        conn.execute(
            "INSERT INTO ledger_entries(id, entry_key, account_id, kind, amount_cents, biz_ref, target_entry_key, booked_at, status, note)"
            " VALUES (1012, 'NT-REV-B2', ?, '冲正', 5000, '', 'NT-MISSING', '2026-11-16T09:10:00Z', '待归并', '')",
            (ACCOUNT_B,),
        )
        conn.commit()

    before = _balances(client)
    session = client.post(
        "/api/netting-sessions",
        json={"account_ids": [ACCOUNT_A, ACCOUNT_B], "entry_ids": [1011, 1012]},
    ).json()
    assert session["status"] == "失败"

    proof = client.get(f"/api/netting-sessions/{session['id']}/proof").json()
    assert proof["outcome"] == "失败"
    assert proof["applied_count"] == 0
    assert "无原单" in (proof["failure_reason"] or "")
    assert {m["account_id"] for m in proof["members"]} == {ACCOUNT_A, ACCOUNT_B}
    assert all(m["applied"] == 0 for m in proof["members"])
    assert _balances(client) == before

    # 不存在的会话：证明接口同样 404
    missing = client.get("/api/netting-sessions/99999/proof")
    assert missing.status_code == 404
