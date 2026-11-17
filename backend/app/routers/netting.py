"""多账户轧差会话接口。"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.db import connect, rows_to_dicts
from app.modules import multi_account_net as svc

router = APIRouter(tags=["多账户轧差"])


class NettingSessionIn(BaseModel):
    account_ids: list[int] = Field(default_factory=list)
    entry_ids: list[int] | None = None
    note: str = ""


@router.get("/netting-sessions")
def get_sessions():
    return svc.list_sessions()


@router.get("/netting-sessions/{session_id}")
def get_session(session_id: int):
    return svc.get_session(session_id)


@router.get("/netting-sessions/{session_id}/report")
def get_session_report(session_id: int):
    return svc.session_report(session_id)


@router.get("/netting-sessions/{session_id}/proof")
def get_session_proof(session_id: int):
    """会话证明只读接口：成员账户、各户净额、会话合计、成败，按会话 id 查询。"""
    return svc.get_session_proof(session_id)


@router.post("/netting-preview")
def post_preview(body: NettingSessionIn):
    """发起前预览会话净额向量（只读，不物化会话）。"""
    return svc.preview_session(body.account_ids, body.entry_ids)


@router.post("/netting-sessions")
def post_session(body: NettingSessionIn):
    return svc.create_session(body.account_ids, body.entry_ids, body.note)


@router.get("/netting-candidates")
def get_candidates(account_ids: str | None = Query(None, description="逗号分隔账户 id")):
    """归并台发起会话前的候选：所选账户的待归并流水。"""
    ids: list[int] | None = None
    if account_ids:
        try:
            ids = [int(x) for x in account_ids.split(",") if x.strip()]
        except ValueError:
            ids = []
    with connect() as conn:
        if ids:
            placeholders = ",".join("?" for _ in ids)
            entries = rows_to_dicts(
                conn.execute(
                    f"""
                    SELECT e.id, e.entry_key, e.account_id, e.kind, e.amount_cents,
                           e.target_entry_key, e.booked_at, e.status,
                           a.code AS account_code
                    FROM ledger_entries e
                    JOIN accounts a ON a.id = e.account_id
                    WHERE e.status = '待归并' AND e.account_id IN ({placeholders})
                    ORDER BY e.account_id, e.id
                    """,
                    ids,
                ).fetchall()
            )
        else:
            entries = rows_to_dicts(
                conn.execute(
                    """
                    SELECT e.id, e.entry_key, e.account_id, e.kind, e.amount_cents,
                           e.target_entry_key, e.booked_at, e.status,
                           a.code AS account_code
                    FROM ledger_entries e
                    JOIN accounts a ON a.id = e.account_id
                    WHERE e.status = '待归并'
                    ORDER BY e.account_id, e.id
                    """
                ).fetchall()
            )
        return {"entries": entries, "accounts": rows_to_dicts(
            conn.execute("SELECT id, code, name FROM accounts ORDER BY id").fetchall()
        )}
