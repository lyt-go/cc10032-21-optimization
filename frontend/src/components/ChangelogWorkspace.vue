<template>
  <section class="cw" data-testid="changelog-workspace">
    <div class="cw-head">
      <h3>对账追溯与日志说明投递</h3>
      <p class="cw-tip">
        对账结果按运行编号回写本清单；在下方输入运行编号或明细编号，可反查当时被比较的实际行与左右侧值。
      </p>
    </div>

    <div class="cw-counts" data-testid="decision-filter-counts">
      <button type="button" class="cw-count cite" :class="{ active: decisionFilter === '继续引用旧差异' }"
              data-testid="decision-filter-cite" @click="toggleDecision('继续引用旧差异')">
        继续引用旧差异 <b data-testid="decision-count-cite">{{ counts['继续引用旧差异'] || 0 }}</b>
      </button>
      <button type="button" class="cw-count withdraw" :class="{ active: decisionFilter === '因更正撤回' }"
              data-testid="decision-filter-withdraw" @click="toggleDecision('因更正撤回')">
        因更正撤回 <b data-testid="decision-count-withdraw">{{ counts['因更正撤回'] || 0 }}</b>
      </button>
      <button v-if="decisionFilter" type="button" class="btn ghost" data-testid="decision-filter-clear"
              @click="toggleDecision('')">清除筛选</button>
    </div>

    <form class="cw-trace-form" data-testid="trace-form" @submit.prevent="runTrace">
      <label>
        <span>运行编号</span>
        <input v-model="traceRunNo" data-testid="trace-run-no" placeholder="如 RUN-0001" />
      </label>
      <label>
        <span>明细编号</span>
        <input v-model="traceDetailNo" data-testid="trace-detail-no" placeholder="如 MX-000001" />
      </label>
      <button class="btn primary" type="submit" data-testid="trace-submit">经编号追查明细</button>
      <span v-if="traceError" class="error-text">{{ traceError }}</span>
    </form>

    <div v-if="trace" class="cw-trace-result" data-testid="trace-result">
      <p>
        运行 <b>{{ trace.run_no }}</b>
        <template v-if="trace.run"> · 结论 {{ trace.run.对账结论 }} · 明细 {{ trace.total }} 条</template>
        · 清单回写 {{ trace.changelog_entries?.length || 0 }} 条
      </p>
      <table v-if="trace.details?.length" class="data-table">
        <thead>
          <tr><th>明细编号</th><th>差异类型</th><th>业务键</th><th>左侧值</th><th>右侧值</th></tr>
        </thead>
        <tbody>
          <tr v-for="d in trace.details" :key="d.id">
            <td>{{ d.明细编号 }}</td>
            <td>{{ d.差异类型 }}</td>
            <td>{{ d.业务键 }}</td>
            <td class="cw-left">{{ d.左侧值 || '—' }}</td>
            <td class="cw-right">{{ d.右侧值 || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <h4 class="cw-sub-title">对账回写 / 日志说明清单</h4>
    <table class="data-table cw-table" data-testid="reconcile-changelog-table">
      <thead>
        <tr>
          <th>记录编号</th><th>运行编号</th><th>明细编号</th><th>业务键</th>
          <th>变更类型</th><th>说明内容</th><th>处理结论</th><th>时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in filteredRows" :key="row.id" :data-testid="`cl-row-${row.id}`"
            :data-decision="row.处理结论 || ''">
          <td>{{ row.记录编号 }}</td>
          <td data-testid="cell-run-no">{{ row.运行编号 || '—' }}</td>
          <td>{{ row.明细编号 || '—' }}</td>
          <td>{{ row.业务键 || '—' }}</td>
          <td><span class="cw-type" :data-type="row.变更类型">{{ row.变更类型 }}</span></td>
          <td class="cw-content" data-testid="cell-content">{{ row.变更内容 }}</td>
          <td data-testid="cell-decision">{{ row.处理结论 || '—' }}</td>
          <td>{{ row.操作时间 }}</td>
        </tr>
        <tr v-if="!filteredRows.length">
          <td colspan="8" class="empty-state">暂无对账回写记录，先在「数据对账」页执行对账或提交日志说明</td>
        </tr>
      </tbody>
    </table>

    <h4 class="cw-sub-title">日志说明草稿 / 投递状态（{{ notes.length }}<template v-if="notes.length > shownNotes.length">，仅显示最近 {{ shownNotes.length }} 条</template>）</h4>
    <table class="data-table" data-testid="notes-table">
      <thead>
        <tr><th>说明编号</th><th>明细编号</th><th>业务键</th><th>状态</th><th>处置结论</th><th>目标日志</th></tr>
      </thead>
      <tbody>
        <tr v-for="n in shownNotes" :key="n.id" :data-testid="`note-row-${n.id}`">
          <td>{{ n.note_no }}</td>
          <td>{{ n.明细编号 }}</td>
          <td>{{ n.业务键 }}</td>
          <td :data-testid="`note-row-status-${n.id}`">{{ n.status }}</td>
          <td>{{ n.处理结论 || '—' }}</td>
          <td>{{ n.target_changelog_no || '—' }}</td>
        </tr>
        <tr v-if="!notes.length"><td colspan="6" class="empty-state">还没有日志说明</td></tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'

import {
  changelogFilterCounts,
  changelogTrace,
  listChangelog,
  listNotes,
} from '@/api/reconcile'

const counts = ref<Record<string, number>>({})
const decisionFilter = ref('')
const rows = ref<any[]>([])
const notes = ref<any[]>([])
const traceRunNo = ref('')
const traceDetailNo = ref('')
const trace = ref<any>(null)
const traceError = ref('')

const filteredRows = computed(() => {
  if (!decisionFilter.value) return rows.value
  return rows.value.filter((r) => r.处理结论 === decisionFilter.value)
})

const shownNotes = computed(() => notes.value.slice(-50).reverse())

async function refreshCountsAndRows() {
  try {
    const fc = await changelogFilterCounts()
    counts.value = fc.decision_counts || {}
  } catch {
    // 计数接口不可用时保持页面其余部分可用
  }
  try {
    const payload = await listChangelog()
    rows.value = (payload.items || []).filter((r: any) => r.运行编号 || r.记录类别)
  } catch {
    rows.value = []
  }
  try {
    const n = await listNotes()
    notes.value = n.items || []
  } catch {
    notes.value = []
  }
}

function toggleDecision(value: string) {
  decisionFilter.value = decisionFilter.value === value ? '' : value
}

async function runTrace() {
  traceError.value = ''
  trace.value = null
  if (!traceRunNo.value.trim() && !traceDetailNo.value.trim()) {
    traceError.value = '请填写运行编号或明细编号'
    return
  }
  try {
    trace.value = await changelogTrace({
      run_no: traceRunNo.value.trim() || undefined,
      detail_no: traceDetailNo.value.trim() || undefined,
    })
  } catch (e) {
    traceError.value = e instanceof Error ? e.message : '追溯查询失败'
  }
}

function onNoteChanged() {
  void refreshCountsAndRows()
}

onMounted(() => {
  window.addEventListener('reconcile:note-changed', onNoteChanged)
  void refreshCountsAndRows()
})
onUnmounted(() => window.removeEventListener('reconcile:note-changed', onNoteChanged))
</script>

<style scoped>
.cw { background: #f8fafc; border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; margin: 14px 0 18px; }
.cw-head h3 { margin: 0 0 4px; font-size: 16px; }
.cw-tip { margin: 0 0 10px; color: var(--muted); font-size: 12.5px; }
.cw-counts { display: flex; gap: 10px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
.cw-count { border: 1px solid var(--border); background: #fff; border-radius: 999px; padding: 6px 14px; font-size: 13px; cursor: pointer; }
.cw-count b { margin-left: 6px; font-size: 14px; }
.cw-count.cite b { color: #15803d; }
.cw-count.withdraw b { color: #b45309; }
.cw-count.active { border-color: var(--brand); box-shadow: 0 0 0 2px rgb(31 111 235 / 15%); }
.cw-trace-form { display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; margin-bottom: 12px; }
.cw-trace-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.cw-trace-form input { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; min-width: 180px; }
.cw-trace-result { margin-bottom: 12px; font-size: 13px; }
.cw-sub-title { font-size: 14px; margin: 14px 0 8px; }
.cw-table, .cw-trace-result table { background: #fff; }
.cw-content { max-width: 420px; font-size: 12.5px; white-space: pre-wrap; }
.cw-left { color: #92400e; }
.cw-right { color: #1e40af; }
.cw-type[data-type='对账差异'] { color: #b42318; font-weight: 600; }
.cw-type[data-type='更正撤回'] { color: #b45309; font-weight: 600; }
</style>
