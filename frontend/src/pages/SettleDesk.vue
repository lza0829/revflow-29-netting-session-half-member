<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getJson, postJson } from '../api'

const route = useRoute()
const router = useRouter()

const accounts = ref([])
const candidates = ref([])
const sessions = ref([])
const runs = ref([])
const report = ref(null)

const selectedAccounts = ref([])
const selectedEntries = ref([])
const note = ref('')
const busy = ref(false)
const loadingReport = ref(false)
const err = ref('')
const msg = ref('')

// 选中会话来自 URL（?session=#），刷新后仍能回到同一会话
const activeSessionId = computed(() => {
  const raw = Number(route.query.session)
  return Number.isInteger(raw) && raw > 0 ? raw : null
})

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}

async function loadBase() {
  const [acc, cand, sess, settleRuns] = await Promise.all([
    getJson('/api/accounts'),
    getJson('/api/netting-candidates'),
    getJson('/api/netting-sessions'),
    getJson('/api/settle-runs'),
  ])
  accounts.value = acc
  candidates.value = cand.entries
  sessions.value = sess
  runs.value = settleRuns
}

async function loadReport() {
  if (!activeSessionId.value) {
    report.value = null
    return
  }
  loadingReport.value = true
  try {
    report.value = await getJson(`/api/netting-sessions/${activeSessionId.value}/report`)
  } catch (e) {
    err.value = String(e.message || e)
    report.value = null
  } finally {
    loadingReport.value = false
  }
}

onMounted(async () => {
  try {
    await loadBase()
    await loadReport()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

watch(activeSessionId, loadReport)

const visibleEntries = computed(() => {
  if (!selectedAccounts.value.length) return []
  const set = new Set(selectedAccounts.value)
  return candidates.value.filter((e) => set.has(e.account_id))
})

// 发起前预览由后端同一套计划逻辑只读计算，与落定规则同源；
// 落定结果仍以会话报告（物化表同源）为准，页面不做内存加总。
const preview = ref(null)
const previewErr = ref('')

async function loadPreview() {
  previewErr.value = ''
  if (selectedAccounts.value.length < 2 || !selectedEntries.value.length) {
    preview.value = null
    return
  }
  try {
    preview.value = await postJson('/api/netting-preview', {
      account_ids: selectedAccounts.value,
      entry_ids: selectedEntries.value,
    })
  } catch (e) {
    preview.value = null
    previewErr.value = String(e.message || e)
  }
}

watch(
  [selectedAccounts, selectedEntries],
  () => {
    loadPreview()
  },
  { deep: true },
)

const allVisibleSelected = computed(
  () =>
    visibleEntries.value.length > 0 &&
    visibleEntries.value.every((e) => selectedEntries.value.includes(e.id)),
)

function toggleAllVisible() {
  if (allVisibleSelected.value) {
    const visible = new Set(visibleEntries.value.map((e) => e.id))
    selectedEntries.value = selectedEntries.value.filter((id) => !visible.has(id))
  } else {
    selectedEntries.value = [
      ...new Set([...selectedEntries.value, ...visibleEntries.value.map((e) => e.id)]),
    ]
  }
}

// 账户取消勾选时，同步剔除其流水，避免跨户提交
watch(
  selectedAccounts,
  (ids) => {
    const set = new Set(ids)
    selectedEntries.value = selectedEntries.value.filter((id) => {
      const e = candidates.value.find((x) => x.id === id)
      return e && set.has(e.account_id)
    })
  },
  { deep: true },
)

async function launch() {
  busy.value = true
  err.value = ''
  msg.value = ''
  try {
    const s = await postJson('/api/netting-sessions', {
      account_ids: selectedAccounts.value,
      entry_ids: selectedEntries.value,
      note: note.value,
    })
    await loadBase()
    if (s.status === '完成') {
      msg.value = `会话 #${s.id} 完成：${s.account_count} 户净额向量一次落定，成员流水 ${s.entry_count} 笔已归并`
      selectedEntries.value = []
      note.value = ''
      preview.value = null
    } else {
      msg.value = ''
      err.value = `会话 #${s.id} 失败闭（${s.failure_reason}）：正式余额与流水均保持会话前`
    }
    await selectSession(s.id)
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function selectSession(id) {
  if (activeSessionId.value === id) {
    await loadReport()
    return
  }
  // watch(activeSessionId) 负责加载；重复点击同一会话时上面直接重取
  await router.replace({ query: { ...route.query, session: id } })
}

async function runSettle() {
  busy.value = true
  err.value = ''
  msg.value = ''
  try {
    const r = await postJson('/api/settle-runs')
    msg.value = `单账户归并 #${r.id}：净额 ${yuan(r.net_cents)}，隔离 ${r.quarantine_count}`
    await loadBase()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

function checkOk(v) {
  return v === true
}

const checkLabels = {
  member_net_matches_entries: '成员净额向量 = 成员流水净额',
  session_net_matches_members: '会话净额 = 各户净额之和',
  after_equals_before_plus_net: '会话后 = 会话前 + 净额',
  applied_all_or_none: '成员余额全落或全不落',
  ledger_matches_members: '正式流水复算 = 成员净额',
  ledger_all_marked: '完成会话的成员流水全部标已归并',
  completed_all_applied: '完成会话的成员余额全部落账',
  current_balance_matches_after: '未被后续改写的成员当前余额 = 会话后',
  failed_no_members_applied: '失败会话没有任何成员被落账',
}
</script>

<template>
  <div>
    <h1>归并台</h1>
    <p class="lead">
      单账户逐户归并会在对向冲正中留下中间余额。轧差会话把 ≥2 个账户与待归并流水作为整体，
      一次算定会话级净额向量并原子切换正式余额。
    </p>
    <p v-if="err" class="msg">{{ err }}</p>
    <p v-if="msg" class="ok">{{ msg }}</p>

    <div class="panel">
      <h2>发起轧差会话</h2>
      <p class="note-strip">
        会话可逐户落定；部分成员失败时仍保留已改余额，会话报告以落库为准。
      </p>

      <div class="field">
        <span class="field-label">参与账户（至少两个）</span>
        <div class="check-grid">
          <label v-for="a in accounts" :key="a.id" class="check-item">
            <input v-model="selectedAccounts" type="checkbox" :value="a.id" />
            <span class="check-code">{{ a.code }}</span>
            <span class="check-name">{{ a.name }}</span>
            <span class="check-bal">¥ {{ yuan(a.balance_cents) }}</span>
          </label>
          <p v-if="!accounts.length" class="muted-line">尚无账户</p>
        </div>
      </div>

      <div class="field">
        <div class="field-head">
          <span class="field-label">待归并流水集合（{{ selectedEntries.length }} / {{ visibleEntries.length }}）</span>
          <button
            v-if="visibleEntries.length"
            class="btn secondary btn-small"
            type="button"
            @click="toggleAllVisible"
          >
            {{ allVisibleSelected ? '清空本区' : '全选本区' }}
          </button>
        </div>
        <table v-if="visibleEntries.length" class="check-table">
          <thead>
            <tr><th></th><th>流水键</th><th>户</th><th>类型</th><th>金额（元）</th><th>原单</th></tr>
          </thead>
          <tbody>
            <tr v-for="e in visibleEntries" :key="e.id">
              <td>
                <input v-model="selectedEntries" type="checkbox" :value="e.id" />
              </td>
              <td class="mono">{{ e.entry_key }}</td>
              <td>{{ e.account_code }}</td>
              <td>
                <span class="tag" :class="e.kind === '正向' ? 'on' : 'warn'">{{ e.kind }}</span>
              </td>
              <td class="mono">{{ yuan(e.amount_cents) }}</td>
              <td class="mono">{{ e.target_entry_key || '—' }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="muted-line">
          先勾选参与账户；只列所选账户状态为「待归并」的流水。
        </p>
      </div>

      <div class="field">
        <span class="field-label">会话备注</span>
        <input v-model="note" class="text-input" placeholder="如：联营对向轧差" />
      </div>

      <div v-if="preview" class="panel inset">
        <h3>会话净额向量预览</h3>
        <p v-if="!preview.ok" class="msg">
          该集合当前无法配平：{{ preview.reasons.join('；') }}
        </p>
        <table>
          <thead>
            <tr><th>户</th><th>会话前（元）</th><th>会话净额（元）</th><th>会话后（元）</th></tr>
          </thead>
          <tbody>
            <tr v-for="p in preview.accounts" :key="p.account_id">
              <td class="mono">{{ p.account_code }}</td>
              <td class="mono">{{ yuan(p.before_balance_cents) }}</td>
              <td class="mono">{{ yuan(p.net_cents) }}</td>
              <td class="mono">{{ yuan(p.after_balance_cents) }}</td>
            </tr>
            <tr>
              <td>合计</td>
              <td></td>
              <td class="mono">{{ yuan(preview.net_total_cents) }}</td>
              <td></td>
            </tr>
          </tbody>
        </table>
        <p class="muted-line">预览由后端同一套计划逻辑只读计算；落定以会话报告（库内同源）为准。</p>
        <p v-if="previewErr" class="msg">{{ previewErr }}</p>
      </div>

      <div class="row-actions">
        <button
          class="btn"
          :disabled="busy || selectedAccounts.length < 2 || !selectedEntries.length"
          @click="launch"
        >
          {{ busy ? '正在轧差…' : '发起轧差会话' }}
        </button>
      </div>
    </div>

    <div class="panel">
      <h2>轧差会话</h2>
      <table>
        <thead>
          <tr>
            <th>#</th><th>状态</th><th>户数</th><th>流水</th><th>净额合计（元）</th><th>发起</th><th>结束</th><th>失败原因</th><th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in sessions" :key="s.id">
            <td>{{ s.id }}</td>
            <td>
              <span class="tag" :class="s.status === '完成' ? 'on' : s.status === '失败' ? 'bad' : 'warn'">
                {{ s.status }}
              </span>
            </td>
            <td>{{ s.account_count }}</td>
            <td>{{ s.entry_count }}</td>
            <td class="mono">{{ yuan(s.net_total_cents) }}</td>
            <td>{{ s.created_at }}</td>
            <td>{{ s.finished_at || '—' }}</td>
            <td class="reason">{{ s.failure_reason || '—' }}</td>
            <td>
              <button class="btn secondary btn-small" type="button" @click="selectSession(s.id)">查看</button>
            </td>
          </tr>
          <tr v-if="!sessions.length"><td colspan="9">尚无轧差会话（先发起一次）</td></tr>
        </tbody>
      </table>
    </div>

    <div v-if="activeSessionId" class="panel" id="netting-result">
      <h2>会话结果 #{{ activeSessionId }}</h2>
      <p v-if="loadingReport" class="muted-line">读取会话报告中…</p>
      <template v-else-if="report">
        <div class="stats">
          <div class="stat">
            <p class="k">会话状态</p>
            <p class="v">{{ report.session.status }}</p>
          </div>
          <div class="stat">
            <p class="k">成员户 / 流水</p>
            <p class="v">{{ report.totals.member_count }} / {{ report.totals.entry_count }}</p>
          </div>
          <div class="stat">
            <p class="k">净额合计（元）</p>
            <p class="v">{{ yuan(report.totals.session_net_cents) }}</p>
          </div>
        </div>

        <p v-if="report.session.failure_reason" class="msg">
          失败原因：{{ report.session.failure_reason }}（已落账成员
          {{ report.totals.applied_count }} 户，正式余额保持会话前）
        </p>

        <h3>逐户会话前后余额</h3>
        <table>
          <thead>
            <tr>
              <th>户</th><th>会话前（元）</th><th>会话净额（元）</th><th>会话后（元）</th><th>当前余额（元）</th><th>落账</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="a in report.accounts" :key="a.account_id">
              <td>{{ a.account_code }} <span class="muted-line">{{ a.account_name }}</span></td>
              <td class="mono">{{ yuan(a.before_balance_cents) }}</td>
              <td class="mono">{{ yuan(a.net_cents) }}</td>
              <td class="mono">{{ yuan(a.after_balance_cents) }}</td>
              <td class="mono">{{ yuan(a.current_balance_cents) }}</td>
              <td>
                <span class="tag" :class="a.applied ? 'on' : 'bad'">{{ a.applied ? '已落' : '未落' }}</span>
              </td>
            </tr>
            <tr>
              <td>合计</td>
              <td class="mono">{{ yuan(report.totals.before_sum_cents) }}</td>
              <td class="mono">{{ yuan(report.totals.member_net_cents) }}</td>
              <td class="mono">{{ yuan(report.totals.after_sum_cents) }}</td>
              <td></td>
              <td></td>
            </tr>
          </tbody>
        </table>

        <h3>成员流水</h3>
        <table>
          <thead>
            <tr><th>流水键</th><th>户</th><th>类型</th><th>金额（元）</th><th>带符号净额（元）</th><th>原单</th></tr>
          </thead>
          <tbody>
            <tr v-for="e in report.entries" :key="e.id">
              <td class="mono">{{ e.entry_key }}</td>
              <td class="mono">#{{ e.account_id }}</td>
              <td>{{ e.kind }}</td>
              <td class="mono">{{ yuan(e.amount_cents) }}</td>
              <td class="mono">{{ yuan(e.signed_cents) }}</td>
              <td class="mono">{{ e.target_entry_key || '—' }}</td>
            </tr>
            <tr v-if="!report.entries.length"><td colspan="6">会话无成员流水</td></tr>
          </tbody>
        </table>

        <h3>同源核对</h3>
        <ul class="check-list">
          <li v-for="(v, k) in report.checks" :key="k">
            <span class="tag" :class="checkOk(v) ? 'on' : 'bad'">{{ checkOk(v) ? '通过' : '不通过' }}</span>
            {{ checkLabels[k] || k }}
          </li>
        </ul>
        <p class="muted-line">数据源：{{ report.source }}；页面不做内存加总。</p>
      </template>
      <p v-else class="muted-line">未找到该会话。</p>
    </div>

    <div class="panel">
      <h2>单账户归并运行</h2>
      <p class="muted-line">
        逐户归并只处理「待归并」流水，用于无对向关系的存量；存在跨户对向冲正时请走轧差会话。
      </p>
      <div class="row-actions">
        <button class="btn secondary" :disabled="busy" @click="runSettle">跑单账户归并</button>
      </div>
      <table>
        <thead>
          <tr>
            <th>#</th><th>状态</th><th>正向</th><th>冲正</th><th>已归并</th><th>隔离</th><th>净额（元）</th><th>结束</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in runs" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.status }}</td>
            <td>{{ r.forward_count }}</td>
            <td>{{ r.reversal_count }}</td>
            <td>{{ r.settled_count }}</td>
            <td>{{ r.quarantine_count }}</td>
            <td class="mono">{{ yuan(r.net_cents) }}</td>
            <td>{{ r.finished_at || '—' }}</td>
          </tr>
          <tr v-if="!runs.length"><td colspan="8">尚无归并运行</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script>
export default {
  name: 'SettleDesk',
}
</script>

<style scoped>
.field { margin: 14px 0 18px; }
.field-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}
.field-label {
  display: block;
  margin-bottom: 8px;
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.12em;
  color: var(--muted);
  text-transform: uppercase;
}
.check-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 4px 18px;
}
.check-item {
  display: grid;
  grid-template-columns: 16px 1fr auto;
  gap: 8px;
  align-items: center;
  padding: 7px 4px;
  border-bottom: 1px solid var(--line);
  font-size: 13px;
  cursor: pointer;
}
.check-code { font-family: var(--mono); font-size: 12px; }
.check-name { color: var(--muted); font-size: 12px; }
.check-bal { font-family: var(--mono); font-size: 12px; text-align: right; }
.check-table th, .check-table td { padding: 8px 6px; }
.mono { font-family: var(--mono); }
.text-input {
  width: 100%;
  max-width: 360px;
  padding: 9px 10px;
  border: 1px solid var(--line);
  background: #fff;
  font: inherit;
  font-size: 13px;
}
.panel.inset { background: #f8fafc; margin: 18px 0; }
.panel h3 {
  margin: 20px 0 10px;
  font-family: var(--display);
  font-size: 15px;
  letter-spacing: 0.08em;
}
.btn-small { padding: 5px 10px; font-size: 12px; }
.muted-line { color: var(--muted); font-size: 12px; }
.reason { max-width: 260px; font-size: 12px; color: var(--danger); }
.check-list { list-style: none; padding: 0; margin: 0; display: grid; gap: 6px; }
.check-list li { font-size: 13px; }
</style>
