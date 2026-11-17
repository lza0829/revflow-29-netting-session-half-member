import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import SettleDesk from '../src/pages/SettleDesk.vue'

const accounts = [
  { id: 1, code: 'ACC-零售-01', name: '华东零售结算户', balance_cents: 0 },
  { id: 4, code: 'ACC-清算-甲', name: '联营清算甲户', balance_cents: 8000 },
  { id: 5, code: 'ACC-清算-乙', name: '联营清算乙户', balance_cents: -8000 },
]
const candidates = {
  entries: [
    { id: 6, entry_key: 'FWD-20261116-006', account_id: 4, kind: '正向', amount_cents: 30000, target_entry_key: null },
    { id: 7, entry_key: 'FWD-20261116-007', account_id: 5, kind: '正向', amount_cents: 22000, target_entry_key: null },
    { id: 12, entry_key: 'REV-20261116-005', account_id: 5, kind: '冲正', amount_cents: 30000, target_entry_key: 'FWD-20261116-006' },
    { id: 13, entry_key: 'REV-20261116-006', account_id: 4, kind: '冲正', amount_cents: 22000, target_entry_key: 'FWD-20261116-007' },
  ],
}
const sessions = [
  { id: 2, status: '完成', account_count: 2, entry_count: 4, net_total_cents: 0, created_at: '2026-11-17T00:00:00Z', finished_at: '2026-11-17T00:00:01Z', failure_reason: null },
  { id: 1, status: '失败', account_count: 2, entry_count: 2, net_total_cents: 52000, created_at: '2026-11-17T00:00:00Z', finished_at: '2026-11-17T00:00:01Z', failure_reason: '冲正 REV-1 无原单' },
]
const proof = {
  id: 1,
  session_id: 2,
  outcome: '完成',
  member_count: 2,
  entry_count: 4,
  net_total_cents: 0,
  before_sum_cents: 0,
  after_sum_cents: 0,
  applied_count: 2,
  failure_reason: null,
  created_at: '2026-11-17T00:00:01Z',
  members: [
    { id: 1, proof_id: 1, session_id: 2, account_id: 4, account_code: 'ACC-清算-甲', before_balance_cents: 0, net_cents: 8000, after_balance_cents: 8000, applied: 1 },
    { id: 2, proof_id: 1, session_id: 2, account_id: 5, account_code: 'ACC-清算-乙', before_balance_cents: 0, net_cents: -8000, after_balance_cents: -8000, applied: 1 },
  ],
}
const report = {
  session: sessions[0],
  accounts: [
    { account_id: 4, account_code: 'ACC-清算-甲', account_name: '联营清算甲户', before_balance_cents: 0, net_cents: 8000, after_balance_cents: 8000, applied: 1, current_balance_cents: 8000 },
    { account_id: 5, account_code: 'ACC-清算-乙', account_name: '联营清算乙户', before_balance_cents: 0, net_cents: -8000, after_balance_cents: -8000, applied: 1, current_balance_cents: -8000 },
  ],
  entries: [
    { id: 6, entry_key: 'FWD-20261116-006', account_id: 4, kind: '正向', amount_cents: 30000, signed_cents: 30000, target_entry_key: null },
  ],
  totals: { member_count: 2, entry_count: 4, applied_count: 2, member_net_cents: 0, entry_signed_cents: 0, before_sum_cents: 0, after_sum_cents: 0, session_net_cents: 0 },
  checks: {
    member_net_matches_entries: true,
    session_net_matches_members: true,
    after_equals_before_plus_net: true,
    applied_all_or_none: true,
    ledger_matches_members: true,
    ledger_all_marked: true,
    completed_all_applied: true,
    current_balance_matches_after: true,
    failed_no_members_applied: true,
  },
  source: 'netting_sessions / netting_session_accounts / netting_session_entries / ledger_entries / accounts',
}

let posted = null
beforeEach(() => {
  posted = null
  globalThis.fetch = vi.fn(async (url, opts) => {
    const u = String(url)
    let body
    if (opts && opts.method === 'POST' && u.includes('/netting-preview')) {
      const req = JSON.parse(opts.body)
      body = {
        ok: true,
        reasons: [],
        accounts: req.account_ids.map((id) => {
          const a = accounts.find((x) => x.id === id)
          const net = req.entry_ids.reduce((sum, eid) => {
            const e = candidates.entries.find((x) => x.id === eid && x.account_id === id)
            if (!e) return sum
            return sum + (e.kind === '正向' ? e.amount_cents : -e.amount_cents)
          }, 0)
          return {
            account_id: id,
            account_code: a.code,
            before_balance_cents: a.balance_cents,
            net_cents: net,
            after_balance_cents: a.balance_cents + net,
          }
        }),
        net_total_cents: 0,
      }
    } else if (opts && opts.method === 'POST') {
      posted = JSON.parse(opts.body)
      body = { ...sessions[0], id: 9, account_count: posted.account_ids.length, entry_count: posted.entry_ids.length }
    } else if (u.includes('/proof')) body = proof
    else if (u.includes('/report')) body = report
    else if (u.includes('/netting-candidates')) body = candidates
    else if (u.includes('/netting-sessions')) body = sessions
    else if (u.includes('/accounts')) body = accounts
    else if (u.includes('/settle-runs')) body = []
    else body = []
    return { ok: true, json: async () => body }
  })
})

async function mountPage(path = '/settle') {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settle', component: SettleDesk }] })
  router.push(path)
  await router.isReady()
  const wrapper = mount(SettleDesk, { global: { plugins: [router] } })
  await flushPromises()
  return { wrapper, router }
}

describe('归并台轧差会话', () => {
  it('从 URL 回到会话结果页（刷新仍在）', async () => {
    const { wrapper } = await mountPage('/settle?session=2')
    const text = wrapper.text()
    expect(text).toContain('会话结果 #2')
    expect(text).toContain('ACC-清算-甲')
    expect(text).toContain('80.00') // 会话净额
    expect(text).toContain('同源核对')
    expect(text).toContain('通过')
  })

  it('会话结果页展示同源物化的会话证明', async () => {
    const { wrapper } = await mountPage('/settle?session=2')
    const text = wrapper.text()
    expect(text).toContain('会话证明 #1')
    expect(text).toContain('成败')
    expect(text).toContain('已落账成员')
    // 证明成员行：各户净额与会话合计来自证明物化表
    const proofCard = wrapper.find('.proof-card')
    expect(proofCard.exists()).toBe(true)
    expect(proofCard.text()).toContain('ACC-清算-甲')
    expect(proofCard.text()).toContain('ACC-清算-乙')
    expect(proofCard.text()).toContain('netting_session_proofs')
  })

  it('发起会话只提交所选账户与流水，并跳转到结果', async () => {
    const { wrapper, router } = await mountPage()
    const accountBoxes = wrapper.findAll('.check-item input[type="checkbox"]')
    await accountBoxes[1].setValue(true)
    await accountBoxes[2].setValue(true)
    await flushPromises()

    const entryBoxes = wrapper.findAll('.check-table input[type="checkbox"]')
    expect(entryBoxes.length).toBe(4)
    for (const box of entryBoxes) await box.setValue(true)

    expect(wrapper.text()).toContain('会话净额向量预览')
    const launch = wrapper.findAll('button').find((b) => b.text().includes('发起轧差会话'))
    expect(launch.attributes('disabled')).toBeUndefined()
    await launch.trigger('click')
    await flushPromises()

    expect(posted).toEqual({
      account_ids: [4, 5],
      entry_ids: [6, 7, 12, 13],
      note: '',
    })
    expect(router.currentRoute.value.query.session).toBe('9')
    expect(wrapper.text()).toContain('会话 #9 完成')
  })

  it('失败会话在列表给出失败原因', async () => {
    const { wrapper } = await mountPage()
    expect(wrapper.text()).toContain('无原单')
    expect(wrapper.findAll('.tag.bad').length).toBeGreaterThan(0)
  })
})
