<template>
  <section class="rw" data-testid="reconcile-workspace">
    <div class="rw-head">
      <h3>对账明细与日志说明</h3>
      <p class="rw-tip">
        按对账运行保存差异类型、业务键、左右侧值与明细编号；分页签发快照，重跑写入不影响已翻到的页。
      </p>
    </div>

    <div class="rw-toolbar">
      <button class="btn primary" data-testid="setup-acceptance" type="button" :disabled="busy"
              @click="onSetupAcceptance">
        构造验收数据（超过一页的金额/状态差异）
      </button>

      <label class="rw-field">
        <span>对账任务</span>
        <select data-testid="task-select" v-model="taskId" @change="onTaskChange">
          <option v-for="t in tasks" :key="t.id" :value="t.id">
            {{ t.任务编号 }} · {{ t.左源表 }} / {{ t.右源表 }}
          </option>
        </select>
      </label>

      <label class="rw-field">
        <span>对账运行</span>
        <select data-testid="run-select" v-model="runNo" @change="onRunChange">
          <option v-for="r in runs" :key="r.run_no" :value="r.run_no">
            {{ r.run_no }} · {{ r.对账结论 }} · 差异 {{ r.diff_count }}
          </option>
        </select>
      </label>

      <button class="btn" data-testid="load-latest" type="button" @click="loadLatestRun">
        查看最新运行
      </button>
      <button class="btn ghost" data-testid="refresh-page" type="button" @click="reloadCurrent">
        刷新本页
      </button>
    </div>

    <div class="rw-filters" data-testid="diff-type-filters">
      <button type="button" class="rw-chip" :class="{ active: diffType === '' }"
              data-testid="diff-filter-all" @click="setDiffType('')">
        全部 <b data-testid="diff-count-all">{{ totalOfRun }}</b>
      </button>
      <button type="button" class="rw-chip amount" :class="{ active: diffType === '金额差异' }"
              data-testid="diff-filter-金额差异" @click="setDiffType('金额差异')">
        金额差异 <b data-testid="diff-count-金额差异">{{ counts['金额差异'] || 0 }}</b>
      </button>
      <button type="button" class="rw-chip status" :class="{ active: diffType === '状态差异' }"
              data-testid="diff-filter-状态差异" @click="setDiffType('状态差异')">
        状态差异 <b data-testid="diff-count-状态差异">{{ counts['状态差异'] || 0 }}</b>
      </button>
      <button v-if="counts['仅左侧存在']" type="button" class="rw-chip"
              :class="{ active: diffType === '仅左侧存在' }"
              data-testid="diff-filter-仅左侧存在" @click="setDiffType('仅左侧存在')">
        仅左侧存在 <b>{{ counts['仅左侧存在'] }}</b>
      </button>
      <button v-if="counts['仅右侧存在']" type="button" class="rw-chip"
              :class="{ active: diffType === '仅右侧存在' }"
              data-testid="diff-filter-仅右侧存在" @click="setDiffType('仅右侧存在')">
        仅右侧存在 <b>{{ counts['仅右侧存在'] }}</b>
      </button>
    </div>

    <p class="rw-meta" data-testid="run-meta">
      <template v-if="runNo">
        当前运行 <b>{{ runNo }}</b>
        <span class="rw-conclusion" :data-conclusion="conclusion">结论：{{ conclusion || '—' }}</span>
        <span data-testid="detail-total">共 {{ total }} 条明细，分页总数与所选运行一致</span>
        <span v-if="pageToken" class="rw-token" data-testid="token-hint">
          已签发稳定分页 {{ pageToken.slice(0, 10) }}…（重跑不影响此分页）
        </span>
      </template>
      <template v-else-if="historicalConclusion">
        历史对账结论：<b>{{ historicalConclusion }}</b>，该任务没有保存运行明细（检索口径已兼容）。
      </template>
      <template v-else>请选择对账任务/运行，或先构造验收数据。</template>
    </p>

    <p v-if="errorMessage" class="error-text" data-testid="workspace-error">{{ errorMessage }}</p>
    <p v-if="infoMessage" class="rw-info" data-testid="workspace-info">{{ infoMessage }}</p>

    <table v-if="details.length" class="data-table rw-table" data-testid="detail-table">
      <thead>
        <tr>
          <th>明细编号</th>
          <th>差异类型</th>
          <th>业务键</th>
          <th>左侧值</th>
          <th>右侧值</th>
          <th>说明状态</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="d in details" :key="String(d.id)" :data-testid="`detail-row-${d.id}`"
            :data-detail-no="d.明细编号" :data-biz-key="d.业务键">
          <td>{{ d.明细编号 }}</td>
          <td>
            <span class="rw-badge" :data-diff-type="d.差异类型">{{ d.差异类型 }}</span>
            <span class="rw-field-name">{{ d.field }}</span>
          </td>
          <td data-testid="cell-biz-key">{{ d.业务键 }}</td>
          <td class="rw-left" data-testid="cell-left-value">{{ d.左侧值 || '—' }}</td>
          <td class="rw-right" data-testid="cell-right-value">{{ d.右侧值 || '—' }}</td>
          <td :data-testid="`note-status-${d.id}`">{{ d.发送状态 || d.note_status || '未发送' }}</td>
          <td class="rw-row-actions">
            <button class="link" type="button" :data-testid="`note-${d.id}`"
                    @click="openNoteModal(d)">
              {{ d.发送状态 === '已发送' ? '查看已发说明' : d.发送状态 === '已撤回' ? '查看撤回说明' : '写日志说明' }}
            </button>
            <button class="link warn" type="button" :data-testid="`correct-row-${d.id}`"
                    :disabled="correctingId === d.id" @click="correctRow(d)">
              {{ correctingId === d.id ? '纠正中…' : '纠正并重跑' }}
            </button>
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="runNo && !details.length && !loading" class="rw-empty">
      当前筛选下没有明细；若该运行结论为「一致」，说明所有业务键均已对齐。
    </div>

    <div v-if="total > 0" class="rw-pager" data-testid="pager">
      <button class="btn" type="button" data-testid="prev-page" :disabled="page <= 1"
              @click="gotoPage(page - 1)">上一页</button>
      <span data-testid="page-info">第 {{ page }} / {{ totalPages }} 页（每页 {{ size }} 条，共 {{ total }} 条）</span>
      <button class="btn" type="button" data-testid="next-page" :disabled="page >= totalPages"
              @click="gotoPage(page + 1)">下一页</button>
      <label class="rw-size">
        每页
        <select v-model.number="size" data-testid="page-size" @change="resetAndLoad">
          <option :value="5">5</option>
          <option :value="10">10</option>
          <option :value="20">20</option>
        </select>
        条
      </label>
    </div>

    <!-- 日志说明弹窗 -->
    <div v-if="showModal" class="modal-backdrop" data-testid="note-modal">
      <div class="modal rw-modal" role="dialog" aria-modal="true">
        <template v-if="!lastResult">
          <h3>日志说明 <span class="rw-note-no" data-testid="note-no">{{ note?.note_no }}</span></h3>

          <div class="rw-old-diff" data-testid="note-old-diff">
            <div><span>引用运行：</span><b>{{ note?.run_no }}</b>　<span>明细编号：</span><b>{{ note?.明细编号 }}</b></div>
            <div><span>业务键：</span><b data-testid="note-biz-key">{{ note?.业务键 }}</b>　<span>差异类型：</span><b>{{ note?.差异类型 }}</b></div>
            <div class="rw-values">
              <span class="rw-left">左侧值：{{ note?.左侧值 || '—' }}</span>
              <span class="rw-right">右侧值：{{ note?.右侧值 || '—' }}</span>
            </div>
          </div>

          <div v-if="stale" class="rw-stale" data-testid="stale-warning">
            ⚠️ 最新对账（{{ staleRunNo || '历史结论' }}）已把业务键「{{ note?.业务键 }}」判为一致。
            提交前请写明本次说明的处置口径：
            <div class="rw-decisions">
              <label class="rw-decision">
                <input type="radio" v-model="decision" value="继续引用旧差异" data-testid="decision-cite" />
                <span><b>继续引用旧差异</b>：仍把 {{ note?.run_no }} / {{ note?.明细编号 }} 的旧差异写入目标日志</span>
              </label>
              <label class="rw-decision">
                <input type="radio" v-model="decision" value="因更正撤回" data-testid="decision-withdraw" />
                <span><b>因更正撤回</b>：该项已在新一轮对账中修正，撤回本说明并留撤回记录</span>
              </label>
            </div>
          </div>
          <div v-else class="rw-valid" data-testid="valid-hint">
            提交前可先纠正源值并重跑，以验证该差异是否仍然成立。
          </div>

          <label class="create-field">
            <span>日志说明内容</span>
            <textarea v-model="content" rows="5" data-testid="note-content"
                      placeholder="写明差异情况、业务键与左右侧值、处置结论…"></textarea>
          </label>

          <p v-if="modalError" class="error-text" data-testid="note-error">{{ modalError }}</p>

          <div class="rw-modal-actions">
            <button class="btn ghost" type="button" data-testid="correct-and-rerun"
                    :disabled="correcting" @click="correctAndRerun">
              {{ correcting ? '纠正并重跑中…' : '纠正右侧值并重跑（模拟源数据修复）' }}
            </button>
            <span class="rw-spacer"></span>
            <button class="btn" type="button" data-testid="save-draft" @click="saveDraft">保存待发草稿</button>
            <button class="btn" type="button" @click="closeModal">取消</button>
            <button class="btn primary" type="button" data-testid="submit-note"
                    :disabled="submitting" @click="onSubmitNote">
              {{ submitting ? '提交中…' : '提交说明' }}
            </button>
          </div>
        </template>

        <template v-else>
          <h3>说明已{{ lastResult.withdrawn ? '撤回' : '提交' }}</h3>
          <div class="rw-success" data-testid="submit-result">
            <p v-if="lastResult.cited_old_diff" class="rw-delivered">
              ✅ 目标日志已收到旧差异：变更日志清单编号
              <b data-testid="target-changelog-no">{{ lastResult.target_changelog_no }}</b>
              （{{ lastResult.target_entry?.变更类型 }} / {{ lastResult.decision }}）
            </p>
            <p v-else-if="lastResult.withdrawn" class="rw-withdrawn">
              ↩️ 已按「因更正撤回」处理，撤回记录编号
              <b>{{ lastResult.target_changelog_no }}</b>
            </p>
            <pre class="rw-target-content" data-testid="target-content">{{ lastResult.target_entry?.变更内容 }}</pre>

            <div class="rw-result-counts" data-testid="result-decision-counts">
              <span>两个筛选条数：</span>
              <span class="rw-chip active">继续引用旧差异 <b>{{ decisionCounts['继续引用旧差异'] || 0 }}</b></span>
              <span class="rw-chip">因更正撤回 <b>{{ decisionCounts['因更正撤回'] || 0 }}</b></span>
            </div>

            <p class="rw-next-range" data-testid="next-send-range">
              {{ lastResult.next_send_range?.label || '暂无后续可发范围' }}
            </p>
          </div>
          <div class="rw-modal-actions">
            <button class="btn" type="button" data-testid="open-next-range"
                    :disabled="!lastResult.next_send_range?.available"
                    @click="openNextRange">
              处理下一段可发说明
            </button>
            <span class="rw-spacer"></span>
            <button class="btn primary" type="button" data-testid="result-done" @click="closeModal">完成</button>
          </div>
        </template>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'

import { listEntries } from '@/api/backend-service'
import {
  correctAndRerun as apiCorrect,
  listDetails,
  listRuns,
  openNote,
  setupAcceptance,
  submitNote,
} from '@/api/reconcile'
import type { EntryRow } from '@/data/types'

const tasks = ref<EntryRow[]>([])
const runs = ref<any[]>([])
const taskId = ref<number | string>('')
const runNo = ref('')
const details = ref<any[]>([])
const total = ref(0)
const page = ref(1)
const size = ref(10)
const totalPages = ref(0)
const pageToken = ref('')
const diffType = ref('')
const counts = ref<Record<string, number>>({})
const conclusion = ref('')
const historicalConclusion = ref('')
const errorMessage = ref('')
const busy = ref(false)
const loading = ref(false)

const showModal = ref(false)
const note = ref<any>(null)
const content = ref('')
const decision = ref('')
const stale = ref(false)
const staleRunNo = ref('')
const modalError = ref('')
const submitting = ref(false)
const correcting = ref(false)
const correctingId = ref<number | string | null>(null)
const infoMessage = ref('')
const lastResult = ref<any>(null)
const decisionCounts = ref<Record<string, number>>({})

const totalOfRun = computed(() => Object.values(counts.value).reduce((a, b) => a + Number(b || 0), 0))

async function loadTasks(selectId?: number | string) {
  const payload = await listEntries('data_reconcile', { size: '200' })
  tasks.value = payload.items
  if (selectId !== undefined && selectId !== null) taskId.value = selectId
  if (!taskId.value && tasks.value.length) taskId.value = tasks.value[0].id
  await loadRuns()
}

async function loadRuns(preferRunNo?: string) {
  if (!taskId.value) return
  const payload = await listRuns(taskId.value)
  runs.value = payload.items || []
  if (preferRunNo) {
    runNo.value = preferRunNo
  } else if (!runNo.value && runs.value.length) {
    runNo.value = runs.value[runs.value.length - 1].run_no
  }
  if (runNo.value) await resetAndLoad()
}

async function onTaskChange() {
  runNo.value = ''
  historicalConclusion.value = ''
  pageToken.value = ''
  await loadRuns()
  if (!runNo.value) await loadHistorical()
}

async function loadHistorical() {
  const data = await listDetails({ task_id: taskId.value, page: 1, size: size.value })
  historicalConclusion.value = data.conclusion || ''
  details.value = []
  total.value = 0
}

async function onRunChange() {
  pageToken.value = ''
  page.value = 1
  await loadPage()
}

async function loadLatestRun() {
  const latest = runs.value[runs.value.length - 1]
  if (latest) {
    runNo.value = latest.run_no
    pageToken.value = ''
    page.value = 1
    diffType.value = ''
    await loadPage()
  }
}

function setDiffType(value: string) {
  diffType.value = value
  page.value = 1
  pageToken.value = ''
  void loadPage()
}

async function resetAndLoad() {
  page.value = 1
  pageToken.value = ''
  await loadPage()
}

async function gotoPage(target: number) {
  if (target < 1 || (totalPages.value && target > totalPages.value)) return
  page.value = target
  await loadPage()
}

async function loadPage() {
  if (!runNo.value) return
  loading.value = true
  errorMessage.value = ''
  try {
    const data = await listDetails({
      run_no: runNo.value,
      page: page.value,
      size: size.value,
      diff_type: diffType.value || undefined,
      // 首次查询不带 token，拿到后持续沿用，保证翻页期间重跑不影响已签发分页
      page_token: pageToken.value || undefined,
    })
    details.value = data.items || []
    total.value = data.total || 0
    totalPages.value = data.total_pages || data.pages || 1
    counts.value = data.filter_counts || {}
    conclusion.value = data.conclusion || ''
    if (data.page_token) pageToken.value = data.page_token
    if (data.page) page.value = data.page
  } catch (e) {
    errorMessage.value = e instanceof Error ? e.message : '明细检索失败'
  } finally {
    loading.value = false
  }
}

async function reloadCurrent() {
  // 沿用 token 重新取本页（明细上的说明状态会刷新）
  await loadPage()
}

async function onSetupAcceptance() {
  busy.value = true
  errorMessage.value = ''
  try {
    const data = await setupAcceptance()
    await loadTasks(data.task_id)
    runNo.value = data.run_no
    pageToken.value = ''
    page.value = 1
    diffType.value = ''
    await loadPage()
  } catch (e) {
    errorMessage.value = e instanceof Error ? e.message : '构造验收数据失败'
  } finally {
    busy.value = false
  }
}

async function correctRow(detail: any) {
  correctingId.value = detail.id
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const result = await apiCorrect({ detail_id: detail.id })
    const payload = await listRuns(taskId.value)
    runs.value = payload.items || []
    infoMessage.value =
      `已纠正业务键 ${detail.业务键} 并重跑生成 ${result.new_run_no}；` +
      `当前页仍固定在 ${runNo.value} 的已签发分页，内容与总数不变。`
    window.dispatchEvent(new CustomEvent('reconcile:note-changed'))
  } catch (e) {
    errorMessage.value = e instanceof Error ? e.message : '纠正并重跑失败'
  } finally {
    correctingId.value = null
  }
}

async function openNoteModal(detail: any) {
  lastResult.value = null
  modalError.value = ''
  decision.value = ''
  try {
    const data = await openNote(detail.id)
    note.value = data.note
    content.value = note.value.content || ''
    stale.value = note.value.stale_check?.state === 'now_consistent'
    staleRunNo.value = note.value.stale_check?.latest_run_no || ''
    showModal.value = true
  } catch (e) {
    errorMessage.value = e instanceof Error ? e.message : '打开日志说明失败'
  }
}

function closeModal() {
  showModal.value = false
  lastResult.value = null
  void reloadCurrent()
  window.dispatchEvent(new CustomEvent('reconcile:note-changed'))
}

async function saveDraft() {
  if (!note.value) return
  await openNote(note.value.detail_id ?? note.value.detailId, content.value)
  modalError.value = ''
  modalError.value = '草稿已保存为「待发送」'
}

async function correctAndRerun() {
  if (!note.value) return
  correcting.value = true
  modalError.value = ''
  try {
    await apiCorrect({ detail_id: note.value.detail_id })
    // 重新打开草稿会刷新 stale_check
    const refreshed = await openNote(note.value.detail_id, content.value)
    note.value = refreshed.note
    stale.value = note.value.stale_check?.state === 'now_consistent'
    staleRunNo.value = note.value.stale_check?.latest_run_no || ''
    // 后台刷新运行列表，但当前页继续沿用旧 run 的稳定 token
    const payload = await listRuns(taskId.value)
    runs.value = payload.items || []
  } catch (e) {
    modalError.value = e instanceof Error ? e.message : '纠正并重跑失败'
  } finally {
    correcting.value = false
  }
}

async function onSubmitNote() {
  if (!note.value) return
  if (stale.value && !decision.value) {
    modalError.value = '该项已被新对账修正：请选择「继续引用旧差异」或「因更正撤回」后再提交。'
    return
  }
  submitting.value = true
  modalError.value = ''
  try {
    const result = await submitNote(note.value.id, {
      content: content.value,
      decision: decision.value || undefined,
      page: page.value,
      size: size.value,
    })
    if (!result.ok) {
      if (result.code === 'stale_decision_required') {
        stale.value = true
        staleRunNo.value = result.state_info?.latest_run_no || ''
        modalError.value = result.reason || '请先选择陈旧差异处置口径'
      } else {
        modalError.value = result.reason || '提交失败'
      }
      return
    }
    lastResult.value = result
    decisionCounts.value = result.decision_counts || {}
    window.dispatchEvent(new CustomEvent('reconcile:note-changed'))
  } catch (e) {
    modalError.value = e instanceof Error ? e.message : '提交说明失败'
  } finally {
    submitting.value = false
  }
}

async function openNextRange() {
  const rng = lastResult.value?.next_send_range
  if (!rng) return
  showModal.value = false
  lastResult.value = null
  if (rng.run_no && rng.run_no !== runNo.value) {
    runNo.value = rng.run_no
    pageToken.value = ''
  }
  const targetPage = rng.scope === 'next_page' ? rng.next_page : rng.page
  page.value = targetPage || 1
  await loadPage()
}

function onModuleAction(event: Event) {
  const detail = (event as CustomEvent).detail || {}
  if (detail.moduleKey !== 'data_reconcile') return
  void loadTasks(detail.id).then(() => loadLatestRun())
}

onMounted(() => {
  window.addEventListener('module-action', onModuleAction as EventListener)
  void loadTasks().then(async () => {
    if (!runNo.value) await loadHistorical()
  })
})
onUnmounted(() => window.removeEventListener('module-action', onModuleAction as EventListener))
</script>

<style scoped>
.rw { background: #f8fafc; border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; margin: 14px 0 18px; }
.rw-head h3 { margin: 0 0 4px; font-size: 16px; }
.rw-tip { margin: 0 0 10px; color: var(--muted); font-size: 12.5px; }
.rw-toolbar { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin-bottom: 10px; }
.rw-field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.rw-field select { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; min-width: 220px; }
.rw-filters { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.rw-chip { border: 1px solid var(--border); background: #fff; border-radius: 999px; padding: 4px 12px; font-size: 13px; cursor: pointer; }
.rw-chip b { margin-left: 4px; }
.rw-chip.active { background: var(--brand); border-color: var(--brand); color: #fff; }
.rw-chip.amount:not(.active) b { color: #b45309; }
.rw-chip.status:not(.active) b { color: #1d4ed8; }
.rw-meta { font-size: 13px; display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin: 6px 0 10px; }
.rw-conclusion[data-conclusion='存在差异'] { color: #b42318; font-weight: 600; }
.rw-conclusion[data-conclusion='一致'] { color: #15803d; font-weight: 600; }
.rw-token { color: var(--muted); font-size: 12px; }
.rw-table { background: #fff; }
.rw-badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px; background: #eef2ff; color: #3730a3; }
.rw-badge[data-diff-type='金额差异'] { background: #fef3c7; color: #92400e; }
.rw-badge[data-diff-type='状态差异'] { background: #dbeafe; color: #1e40af; }
.rw-field-name { color: var(--muted); font-size: 12px; margin-left: 6px; }
.rw-left { color: #92400e; }
.rw-right { color: #1e40af; }
.rw-row-actions { white-space: nowrap; }
.rw-row-actions .link { margin-right: 10px; }
.link.warn { color: #b45309; }
.rw-info { color: #1d4ed8; background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 6px; padding: 6px 10px; font-size: 12.5px; margin: 4px 0 8px; }
.rw-pager { display: flex; align-items: center; gap: 12px; margin-top: 10px; font-size: 13px; }
.rw-size { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 12px; }
.rw-size select { padding: 3px 6px; }
.rw-empty { padding: 18px; text-align: center; color: var(--muted); background: #fff; border: 1px dashed var(--border); border-radius: 8px; }
.rw-modal { width: min(640px, 100%); }
.rw-note-no { color: var(--muted); font-size: 13px; font-weight: 400; }
.rw-old-diff { background: #f8fafc; border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; font-size: 13px; display: grid; gap: 6px; margin-bottom: 12px; }
.rw-values { display: flex; gap: 16px; flex-wrap: wrap; }
.rw-stale { background: #fffbeb; border: 1px solid #f59e0b; border-radius: 8px; padding: 10px 12px; font-size: 13px; color: #92400e; margin-bottom: 12px; }
.rw-valid { font-size: 12.5px; color: var(--muted); margin-bottom: 10px; }
.rw-decisions { display: grid; gap: 8px; margin-top: 8px; }
.rw-decision { display: flex; gap: 8px; align-items: flex-start; color: #1f2937; }
.rw-decision input { margin-top: 3px; }
.create-field textarea { width: 100%; box-sizing: border-box; border: 1px solid var(--border); border-radius: 6px; padding: 8px; font-family: inherit; font-size: 13px; }
.rw-modal-actions { display: flex; gap: 8px; align-items: center; margin-top: 12px; flex-wrap: wrap; }
.rw-spacer { flex: 1; }
.rw-success { font-size: 13.5px; display: grid; gap: 10px; }
.rw-delivered { color: #15803d; margin: 0; }
.rw-withdrawn { color: #b45309; margin: 0; }
.rw-target-content { background: #0f172a; color: #e2e8f0; padding: 10px 12px; border-radius: 8px; font-size: 12.5px; white-space: pre-wrap; margin: 0; }
.rw-result-counts { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.rw-next-range { background: #eff6ff; border: 1px solid #93c5fd; color: #1e40af; border-radius: 8px; padding: 8px 12px; margin: 0; }
</style>
