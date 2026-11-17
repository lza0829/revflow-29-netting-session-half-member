# 07-revflow（冲流台）

冲流台 — 正向与冲正流水净额归并：正向装载 → 冲正关联入账 → 净额归并落余额 → 未平/超额隔离 → 流水净额与余额互证。

**不是** 多源 Feed 归并，**不是** 作业依赖编排，**不是** CDC 变更流，**不是** 质检签发，**不是** 数仓分层建设，**不是** 交班过户。  
**也不是** 收银/退款 CRUD 后台、电商订单、报表 CSV 看板——主链是**流水管道与净额状态**，不是业务单据编辑台。

## 启动

```bash
# 后端
cd backend
pip install -r requirements.txt
set DATA_DIR=%CD%\data
set FIXTURES_DIR=%CD%\..\fixtures
uvicorn app.main:app --host 127.0.0.1 --port 10700

# 前端
cd frontend
npm install
npm run dev
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://127.0.0.1:5700 |
| 接口 | http://127.0.0.1:10700 |

## 主链

账户登记 → 装载正向流水 → 装载冲正并关联原单 → 跑净额归并（已配平落余额，未平/超额进隔离）→ 互证报告核对「流水净额 vs 账户余额」

## 多账户轧差会话

逐户归并只按单账户循环，存在跨户对向冲正时会留下多笔中间余额。归并台另有**轧差会话**：选定 ≥2 个参与户与待归并流水集合，一次算定**会话级净额向量**并原子落定。

- **物化形态**：独立轧差会话表 `netting_sessions` + 成员账户快照 `netting_session_accounts`（会话前/净额/会话后/是否落账）+ 成员流水外键 `netting_session_entries`（`entry_id` 外键指向 `ledger_entries`）；成员流水与账户分别挂 `netting_session_id` / `last_netting_session_id`。
- **原子切换**：先只读校验算影子草稿；通过后 `BEGIN IMMEDIATE` 内复核成员流水仍待归并、账户余额仍等于会话前快照，再一次写入全部参与户余额并统一标已归并，提交前任何一步失败整体回滚。
- **失败闭**：任一成员无原单、超额、流水已被占用或不属所选户，则整会话失败，物化失败会话与成员快照供追溯，正式余额与流水状态保持会话前。
- **报告同源**：会话报告逐户给出会话前 / 会话净额 / 会话后 / 当前余额，净额与流水由 SQL 在物化行与正式表上复算，并列出同源核对项；页面不做内存加总。
- **入口**：归并台「发起轧差会话」与「会话结果」；结果页锚定 `?session=<id>`，刷新后会话仍在。

接口：`GET/POST /api/netting-sessions`、`GET /api/netting-sessions/{id}`、`GET /api/netting-sessions/{id}/report`、`POST /api/netting-preview`、`GET /api/netting-candidates`。

单账户归并改为**增量落账**（`balance_cents + 本轮净额`），不会覆盖轧差会话已落定的余额；同时清空账户的 `last_netting_session_id` 指针，会话报告据此区分「余额仍等于会话后」的成员。

## 测试

```bash
cd backend && pytest          # 后端 9 例：会话落定 / 失败闭 / 超额 / 只读预览 / 会话后增量归并
cd frontend && npm test       # 归并台组件 3 例：URL 回会话 / 发起提交 / 失败原因
```

## 界面约定

页面文案全部中文；入口：总览 / 账户 / 正向流水 / 冲正流水 / 归并台 / 未平隔离 / 净额互证。  
视觉：**左轨编号导航 + 右侧账册横线工作面**（Literata / JetBrains Mono，靛蓝强调）；**不是**交班台那种顶栏品牌+横向流程条。

## 与已有仓差异

| 仓 | 骨架 |
| --- | --- |
| Feedstitch | 多源解析→冲突隔离→水位 |
| Batchgate | 作业依赖→重试→水位 |
| Changeflow | 事件序→缺口→Sink 游标 |
| Checklane | 入检→规则→隔离签发 |
| Tiercraft | 贴源→明细→汇总→层间对账 |
| Shiftboard | 班次归属→交班互斥→批次过户 |
| **本仓** | **正向装载→冲正关联→净额归并→未平隔离→净额互证** |
