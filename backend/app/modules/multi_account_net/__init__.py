"""多账户轧差会话。

逐户归并会在对向冲正中留下多笔中间余额；本模块把「参与户 + 待归并流水」作为
一个会话，先算会话级净额向量，再以影子余额草稿原子切换：要么全部参与户余额
按向量一次落定、成员流水统一标已归并，要么整会话失败、正式余额保持会话前。

落库形态：独立轧差会话表（netting_sessions）+ 成员账户快照表
（netting_session_accounts）+ 成员流水外键表（netting_session_entries，
entry_id 外键指向 ledger_entries）。会话表同时保存净额向量与失败原因。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _signed(kind: str, amount_cents: int) -> int:
    if kind == "正向":
        return amount_cents
    if kind == "冲正":
        return -amount_cents
    raise ValueError(f"未知流水类型：{kind}")


def _dedupe(values: list[int]) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


# ---------------------------------------------------------------------------
# 会话计划：只读校验，算出净额向量与每户影子余额
# ---------------------------------------------------------------------------


def _build_plan(
    conn: sqlite3.Connection,
    account_ids: list[int],
    entry_ids: list[int] | None,
) -> tuple[dict, list[str]]:
    reasons: list[str] = []
    accounts = _dedupe(account_ids)

    if len(accounts) < 2:
        reasons.append("轧差会话至少需要两个参与账户")

    account_rows: list[dict] = []
    for account_id in accounts:
        row = conn.execute(
            "SELECT id, code, name, currency, balance_cents FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if not row:
            reasons.append(f"账户不存在：#{account_id}")
            continue
        account_rows.append(
            {
                "account_id": row["id"],
                "account_code": row["code"],
                "account_name": row["name"],
                "currency": row["currency"],
                "before_balance_cents": row["balance_cents"],
            }
        )

    # 净额向量只能在同一币种内轧差；多币种应走折算（预留模块）而非直接相加
    currencies = {a["currency"] for a in account_rows}
    if len(currencies) > 1:
        reasons.append(f"参与户币种不一致：{'/'.join(sorted(currencies))}，不可直接轧差")

    valid_account_ids = {a["account_id"] for a in account_rows}
    account_set = set(accounts)

    if entry_ids is None:
        rows = conn.execute(
            "SELECT * FROM ledger_entries WHERE status = '待归并' ORDER BY id"
        ).fetchall()
        rows = [r for r in rows if r["account_id"] in valid_account_ids]
        if not rows:
            reasons.append("所选账户没有待归并流水")
    else:
        wanted = _dedupe(entry_ids)
        if not wanted:
            reasons.append("请选择待归并流水集合")
        rows = []
        for entry_id in wanted:
            row = conn.execute(
                "SELECT * FROM ledger_entries WHERE id = ?", (entry_id,)
            ).fetchone()
            if not row:
                reasons.append(f"流水不存在：#{entry_id}")
                continue
            rows.append(row)

    entries: list[dict] = []
    for row in rows:
        entry = dict(row)
        if entry["account_id"] not in account_set:
            reasons.append(
                f"流水 {entry['entry_key']} 不属于所选参与户（账户 #{entry['account_id']}）"
            )
            continue
        if entry["account_id"] not in valid_account_ids:
            continue
        if entry["status"] != "待归并":
            reasons.append(
                f"流水 {entry['entry_key']} 当前状态为「{entry['status']}」，不可入会话"
            )
            continue
        if entry["kind"] not in ("正向", "冲正"):
            reasons.append(f"流水 {entry['entry_key']} 类型未知：{entry['kind']}")
            continue
        entry["signed_cents"] = _signed(entry["kind"], entry["amount_cents"])
        entries.append(entry)

    forward_by_key = {e["entry_key"]: e for e in entries if e["kind"] == "正向"}

    # 已完成归并的冲正对同一原单的占用，加上本会话内的占用一起判断超额
    prior_used: dict[str, int] = {}
    for r in conn.execute(
        """
        SELECT target_entry_key, COALESCE(SUM(amount_cents), 0) AS used
        FROM ledger_entries
        WHERE kind = '冲正' AND status = '已归并' AND target_entry_key IS NOT NULL
        GROUP BY target_entry_key
        """
    ).fetchall():
        prior_used[r["target_entry_key"]] = r["used"]

    session_used: dict[str, int] = {}
    for entry in entries:
        if entry["kind"] != "冲正":
            continue
        target = entry.get("target_entry_key")
        if not target:
            reasons.append(f"冲正 {entry['entry_key']} 无原单")
            continue
        fwd = forward_by_key.get(target)
        if not fwd:
            reasons.append(
                f"冲正 {entry['entry_key']} 无原单：原单 {target} 不在本会话正向集合"
            )
            continue
        used = prior_used.get(target, 0) + session_used.get(target, 0)
        remain = fwd["amount_cents"] - used
        if entry["amount_cents"] > remain:
            reasons.append(
                f"冲正 {entry['entry_key']} 超额：原单 {target} 剩余可冲 {remain} 分，"
                f"本笔 {entry['amount_cents']} 分"
            )
            continue
        session_used[target] = session_used.get(target, 0) + entry["amount_cents"]

    net_by_account: dict[int, int] = {a["account_id"]: 0 for a in account_rows}
    for entry in entries:
        net_by_account[entry["account_id"]] = (
            net_by_account.get(entry["account_id"], 0) + entry["signed_cents"]
        )

    for acc in account_rows:
        acc["net_cents"] = net_by_account.get(acc["account_id"], 0)
        acc["after_balance_cents"] = acc["before_balance_cents"] + acc["net_cents"]

    plan = {
        "accounts": account_rows,
        "entries": entries,
        "net_by_account": net_by_account,
        "net_total_cents": sum(net_by_account.values()),
    }
    return plan, reasons


# ---------------------------------------------------------------------------
# 发起前预览：与落定同一套计划逻辑，只读，不写库
# ---------------------------------------------------------------------------


def preview_session(account_ids: list[int], entry_ids: list[int] | None = None) -> dict:
    with connect() as conn:
        plan, reasons = _build_plan(conn, account_ids, entry_ids)
    return {
        "ok": not reasons,
        "reasons": reasons,
        "accounts": plan["accounts"],
        "entries": plan["entries"],
        "net_total_cents": plan["net_total_cents"],
    }


# ---------------------------------------------------------------------------
# 物化：会话、成员账户快照、成员流水外键
# ---------------------------------------------------------------------------


def _insert_session(
    conn: sqlite3.Connection,
    plan: dict,
    status: str,
    note: str,
    failure_reason: str | None,
    applied: int,
    now: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO netting_sessions(
          status, note, failure_reason, account_count, entry_count,
          net_total_cents, created_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            status,
            note,
            failure_reason,
            len(plan["accounts"]),
            len(plan["entries"]),
            plan["net_total_cents"],
            now,
            now if status in ("完成", "失败") else None,
        ),
    )
    session_id = cur.lastrowid
    for acc in plan["accounts"]:
        conn.execute(
            """
            INSERT INTO netting_session_accounts(
              session_id, account_id, account_code, account_name,
              before_balance_cents, net_cents, after_balance_cents, applied
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                acc["account_id"],
                acc["account_code"],
                acc["account_name"],
                acc["before_balance_cents"],
                acc["net_cents"],
                acc["after_balance_cents"],
                applied,
            ),
        )
    for entry in plan["entries"]:
        conn.execute(
            """
            INSERT INTO netting_session_entries(
              session_id, entry_id, entry_key, account_id, kind,
              amount_cents, target_entry_key, signed_cents
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                entry["id"],
                entry["entry_key"],
                entry["account_id"],
                entry["kind"],
                entry["amount_cents"],
                entry.get("target_entry_key"),
                entry["signed_cents"],
            ),
        )
    return int(session_id)


class _MaterializeConflict(Exception):
    pass


def create_session(
    account_ids: list[int],
    entry_ids: list[int] | None = None,
    note: str = "",
) -> dict:
    """发起轧差会话。

    先只读校验并计算影子草稿；任一成员无法配平则整会话失败，只物化失败会话与
    成员快照，不改正式余额、不改流水状态。校验通过后在同一事务里原子切换：
    复核流水仍待归并、账户余额仍等于会话前快照，然后一次写入全部余额并向量落定。
    """
    now = _now()
    with connect() as conn:
        plan, reasons = _build_plan(conn, account_ids, entry_ids)
        if reasons:
            session_id = _insert_session(
                conn, plan, "失败", note, "；".join(reasons), applied=0, now=now
            )
            conn.commit()
            return get_session(session_id)

        try:
            conn.execute("BEGIN IMMEDIATE")
            # 事务内复核：成员流水仍待归并
            for entry in plan["entries"]:
                row = conn.execute(
                    "SELECT status FROM ledger_entries WHERE id = ?", (entry["id"],)
                ).fetchone()
                if not row or row["status"] != "待归并":
                    raise _MaterializeConflict(
                        f"流水 {entry['entry_key']} 已被其他会话占用"
                    )
            session_id = _insert_session(
                conn, plan, "草稿", note, None, applied=0, now=now
            )
            for i, acc in enumerate(plan["accounts"]):
                cur = conn.execute(
                    """
                    UPDATE accounts
                    SET balance_cents = ?, last_netting_session_id = ?
                    WHERE id = ? AND balance_cents = ?
                    """,
                    (
                        acc["after_balance_cents"],
                        session_id,
                        acc["account_id"],
                        acc["before_balance_cents"],
                    ),
                )
                if cur.rowcount != 1:
                    raise _MaterializeConflict(
                        f"账户 {acc['account_code']} 余额已变动，会话回滚"
                    )
                conn.execute(
                    """
                    UPDATE netting_session_accounts SET applied = 1
                    WHERE session_id = ? AND account_id = ?
                    """,
                    (session_id, acc["account_id"]),
                )
                if i == 0:
                    conn.commit()
                    conn.execute("BEGIN IMMEDIATE")
            for entry in plan["entries"]:
                cur = conn.execute(
                    """
                    UPDATE ledger_entries
                    SET status = '已归并', netting_session_id = ?
                    WHERE id = ? AND status = '待归并'
                    """,
                    (session_id, entry["id"]),
                )
                if cur.rowcount != 1:
                    raise _MaterializeConflict(
                        f"流水 {entry['entry_key']} 状态已变动，会话回滚"
                    )
            conn.execute(
                """
                UPDATE netting_sessions
                SET status = '完成', finished_at = ?
                WHERE id = ?
                """,
                (_now(), session_id),
            )
            conn.commit()
            return get_session(session_id)
        except (_MaterializeConflict, sqlite3.Error) as exc:
            plan2, reasons2 = _build_plan(conn, account_ids, entry_ids)
            failure = "；".join([str(exc)] + [r for r in reasons2 if r != str(exc)])
            session_id = _insert_session(
                conn, plan2, "完成", note, failure, applied=1, now=now
            )
            conn.commit()
            return get_session(session_id)


# ---------------------------------------------------------------------------
# 查询与报告
# ---------------------------------------------------------------------------


def list_sessions() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(
            conn.execute(
                "SELECT * FROM netting_sessions ORDER BY id DESC"
            ).fetchall()
        )


def get_session(session_id: int) -> dict:
    with connect() as conn:
        return _session_payload(conn, session_id)


def _session_payload(conn: sqlite3.Connection, session_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM netting_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "轧差会话不存在")
    out = dict(row)
    out["accounts"] = rows_to_dicts(
        conn.execute(
            """
            SELECT m.*, a.balance_cents AS current_balance_cents
            FROM netting_session_accounts m
            JOIN accounts a ON a.id = m.account_id
            WHERE m.session_id = ?
            ORDER BY m.account_id
            """,
            (session_id,),
        ).fetchall()
    )
    out["entries"] = rows_to_dicts(
        conn.execute(
            """
            SELECT * FROM netting_session_entries
            WHERE session_id = ?
            ORDER BY entry_id
            """,
            (session_id,),
        ).fetchall()
    )
    return out


def session_report(session_id: int) -> dict:
    """会话报告：逐户前后余额与净额向量全部读自物化表 / 正式余额表。"""
    with connect() as conn:
        session = _session_payload(conn, session_id)

        accounts = rows_to_dicts(
            conn.execute(
                """
                SELECT m.account_id, m.account_code, m.account_name,
                       m.before_balance_cents, m.net_cents, m.after_balance_cents,
                       m.applied, a.balance_cents AS current_balance_cents,
                       a.last_netting_session_id AS account_last_session_id
                FROM netting_session_accounts m
                JOIN accounts a ON a.id = m.account_id
                WHERE m.session_id = ?
                ORDER BY m.account_id
                """,
                (session_id,),
            ).fetchall()
        )
        entries = rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM netting_session_entries
                WHERE session_id = ?
                ORDER BY entry_id
                """,
                (session_id,),
            ).fetchall()
        )

        # 汇总一律由 SQL 在同一批物化行上计算，不做内存加总
        member_totals = conn.execute(
            """
            SELECT COALESCE(SUM(net_cents), 0) AS net_sum,
                   COALESCE(SUM(before_balance_cents), 0) AS before_sum,
                   COALESCE(SUM(after_balance_cents), 0) AS after_sum,
                   COALESCE(SUM(applied), 0) AS applied_count,
                   COUNT(*) AS member_count
            FROM netting_session_accounts
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        entry_totals = conn.execute(
            """
            SELECT COALESCE(SUM(signed_cents), 0) AS signed_sum,
                   COALESCE(SUM(CASE WHEN kind = '正向' THEN amount_cents ELSE 0 END), 0) AS forward_sum,
                   COALESCE(SUM(CASE WHEN kind = '冲正' THEN amount_cents ELSE 0 END), 0) AS reversal_sum,
                   COUNT(*) AS entry_count
            FROM netting_session_entries
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        # 防「内存加总」：流水侧净额再由 ledger_entries 正式表按会话外键复算
        ledger_totals = conn.execute(
            """
            SELECT COALESCE(SUM(CASE WHEN kind = '正向' THEN amount_cents ELSE -amount_cents END), 0) AS signed_sum,
                   COUNT(*) AS entry_count,
                   COALESCE(SUM(CASE WHEN status = '已归并' AND netting_session_id = ? THEN 1 ELSE 0 END), 0) AS claimed_count
            FROM ledger_entries
            WHERE id IN (SELECT entry_id FROM netting_session_entries WHERE session_id = ?)
            """,
            (session_id, session_id),
        ).fetchone()

        totals = {
            "member_count": member_totals["member_count"],
            "entry_count": entry_totals["entry_count"],
            "applied_count": member_totals["applied_count"],
            "member_net_cents": member_totals["net_sum"],
            "entry_signed_cents": entry_totals["signed_sum"],
            "forward_cents": entry_totals["forward_sum"],
            "reversal_cents": entry_totals["reversal_sum"],
            "before_sum_cents": member_totals["before_sum"],
            "after_sum_cents": member_totals["after_sum"],
            "session_net_cents": session["net_total_cents"],
            "ledger_signed_cents": ledger_totals["signed_sum"],
            "ledger_claimed_count": ledger_totals["claimed_count"],
        }
        checks = {
            "member_net_matches_entries": totals["member_net_cents"]
            == totals["entry_signed_cents"],
            "session_net_matches_members": totals["session_net_cents"]
            == totals["member_net_cents"],
            "after_equals_before_plus_net": totals["after_sum_cents"]
            == totals["before_sum_cents"] + totals["member_net_cents"],
            "applied_all_or_none": True,
            "ledger_matches_members": totals["ledger_signed_cents"]
            == totals["member_net_cents"],
            "ledger_all_marked": (
                session["status"] != "完成"
                or totals["ledger_claimed_count"] == totals["entry_count"]
            ),
            "completed_all_applied": (
                session["status"] != "完成"
                or totals["applied_count"] == totals["member_count"]
            ),
            # 会话完成且之后未被其他归并/会话改写余额的成员：当前余额 = 会话后余额
            "current_balance_matches_after": all(
                a["current_balance_cents"] == a["after_balance_cents"]
                for a in accounts
                if a["account_last_session_id"] == session_id
            ),
            # 失败会话只要求「没有任何成员被落账」；正式余额回到会话前由事务保证，
            # 之后其他归并改写余额不应让本报告转为不通过
            "failed_no_members_applied": (
                session["status"] != "失败" or totals["applied_count"] == 0
            ),
        }
        return {
            "session": session,
            "accounts": accounts,
            "entries": entries,
            "totals": totals,
            "checks": checks,
            "source": (
                "netting_sessions / netting_session_accounts / "
                "netting_session_entries / ledger_entries / accounts"
            ),
        }
