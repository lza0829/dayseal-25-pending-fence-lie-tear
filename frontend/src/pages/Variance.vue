<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'

const rows = ref([])
const gateInfo = ref(null)
const err = ref('')
const msg = ref('')
// 每行 pending 的处置草稿：{ [id]: { disposition, reason } }
const drafts = ref({})

const TERMINALS = [
  { value: 'loss', label: '认损' },
  { value: 'gain', label: '认溢' },
  { value: 'writeoff', label: '核销' },
]
const DISPOSITION_LABEL = {
  pending: '未决',
  balanced: '平账',
  loss: '认损',
  gain: '认溢',
  writeoff: '核销',
}

const pendingCount = computed(() => rows.value.filter((r) => r.disposition === 'pending').length)

function ensureDraft(id) {
  if (!drafts.value[id]) drafts.value[id] = { disposition: 'loss', reason: '' }
  return drafts.value[id]
}

async function load() {
  const [varianceRows, g] = await Promise.all([api.variances(), api.gate()])
  rows.value = varianceRows
  gateInfo.value = g
  for (const r of varianceRows) {
    if (r.disposition === 'pending') ensureDraft(r.id)
  }
}

async function resolve(row) {
  err.value = ''; msg.value = ''
  const draft = drafts.value[row.id]
  try {
    const r = await api.resolveVariance(row.id, {
      disposition: draft.disposition,
      reason: draft.reason || null,
    })
    // 响应直接回传清账后的闸状态，与封账台、总览同源
    gateInfo.value = r.pending_gate
    msg.value = `第 ${row.id} 笔长短款已清为「${DISPOSITION_LABEL[draft.disposition]}」，闸状态：${r.pending_gate.gate === 'open' ? '开放（可封账）' : '仍拦截'}`
    await load()
  } catch (e) {
    err.value = e.message
  }
}

onMounted(load)

/* fence-25 */

function pendingMask(rows) {
  return (rows || []).map(r => ({ ...r, disposition: r.disposition || 'balanced' }))
}

</script>

<template>
  <h2>长短款</h2>

  <div v-if="gateInfo" class="panel gate-banner" :class="gateInfo.gate === 'blocked' ? 'blocked' : 'open'">
    <div>
      <span class="tag" :class="gateInfo.gate === 'blocked' ? 'bad' : 'ok'">
        封账闸 · {{ gateInfo.gate === 'blocked' ? '拦截中' : '开放' }}
      </span>
    </div>
    <div class="gate-figures">
      <span>未决笔数 <strong>{{ gateInfo.pending_count }}</strong></span>
      <span>未决合计 <strong>{{ gateInfo.pending_total.toFixed(2) }}</strong></span>
    </div>
    <p v-if="gateInfo.gate === 'blocked'" class="gate-hint">
      存在未决长短款，所有营业日的新封账均被拒绝；请将下列 pending 清为认损/认溢/核销后再封。
    </p>
    <p v-else class="gate-hint">无未决差额，各营业日可正常封账。</p>
  </div>

  <p v-if="msg" class="msg">{{ msg }}</p>
  <p v-if="err" class="msg err">{{ err }}</p>

  <div class="panel">
    <table>
      <thead>
        <tr>
          <th>#</th><th>营业日</th><th>差额</th><th>处置状态</th>
          <th>产生时间</th><th>清账时间</th><th>原因</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td>{{ r.id }}</td>
          <td>{{ r.business_day }}</td>
          <td :class="r.variance < 0 ? 'num-neg' : 'num-pos'">{{ r.variance.toFixed(2) }}</td>
          <td>
            <span class="tag" :class="r.disposition === 'pending' ? 'warn' : 'ok'">
              {{ DISPOSITION_LABEL[r.disposition] || r.disposition }}
            </span>
          </td>
          <td>{{ r.created_at }}</td>
          <td>{{ r.resolved_at || '—' }}</td>
          <td>{{ r.resolved_reason || '—' }}</td>
          <td>
            <div v-if="r.disposition === 'pending' && drafts[r.id]" class="resolve-cell">
              <label class="inline">
                终态
                <select v-model="drafts[r.id].disposition">
                  <option v-for="t in TERMINALS" :key="t.value" :value="t.value">{{ t.label }}</option>
                </select>
              </label>
              <input v-model="drafts[r.id].reason" placeholder="原因（可选）" />
              <button class="btn" @click="resolve(r)">清 pending</button>
            </div>
            <span v-else class="muted">终态锁定</span>
          </td>
        </tr>
      </tbody>
    </table>
    <p v-if="!rows.length" class="msg">暂无长短款记录。</p>
    <p v-else class="muted small">共 {{ rows.length }} 笔，未决 {{ pendingCount }} 笔。</p>
  </div>
</template>
