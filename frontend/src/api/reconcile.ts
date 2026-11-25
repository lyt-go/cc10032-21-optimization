/** 对账明细、稳定分页、日志说明与追溯接口封装。 */
import { request } from './client'

async function jsonOrThrow(response: Response): Promise<any> {
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(data?.detail || `接口返回 ${response.status}`)
  return data
}

export type DetailQuery = {
  run_no?: string
  task_id?: number | string
  page?: number
  size?: number
  diff_type?: string
  page_token?: string
}

export async function setupAcceptance(): Promise<any> {
  const resp = await request('/api/reconcile/setup-acceptance', {
    method: 'POST',
    body: JSON.stringify({ total: 23, amount_count: 12 }),
  })
  return jsonOrThrow(resp)
}

export async function listRuns(taskId?: number | string): Promise<any> {
  const qs = taskId ? `?task_id=${encodeURIComponent(String(taskId))}` : ''
  const resp = await request(`/api/reconcile/runs${qs}`)
  return jsonOrThrow(resp)
}

export async function listDetails(query: DetailQuery): Promise<any> {
  const params = new URLSearchParams()
  Object.entries(query).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value))
  })
  const resp = await request(`/api/reconcile/details?${params.toString()}`)
  return jsonOrThrow(resp)
}

export async function correctAndRerun(payload: {
  detail_id?: number | string
  biz_key?: string
}): Promise<any> {
  const resp = await request('/api/reconcile/correct', {
    method: 'POST',
    body: JSON.stringify({ ...payload, rerun: true }),
  })
  return jsonOrThrow(resp)
}

export async function openNote(detailId: number | string, content?: string): Promise<any> {
  const resp = await request(`/api/reconcile/details/${detailId}/note`, {
    method: 'POST',
    body: JSON.stringify(content ? { content } : {}),
  })
  return jsonOrThrow(resp)
}

export async function submitNote(
  noteId: number | string,
  payload: { content?: string; decision?: string; page?: number; size?: number },
): Promise<any> {
  const resp = await request(`/api/reconcile/notes/${noteId}/submit`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return jsonOrThrow(resp)
}

export async function listNotes(): Promise<any> {
  const resp = await request('/api/reconcile/notes')
  return jsonOrThrow(resp)
}

export async function changelogTrace(query: { run_no?: string; detail_no?: string }): Promise<any> {
  const params = new URLSearchParams()
  if (query.run_no) params.set('run_no', query.run_no)
  if (query.detail_no) params.set('detail_no', query.detail_no)
  const resp = await request(`/api/changelog/trace?${params.toString()}`)
  return jsonOrThrow(resp)
}

export async function changelogFilterCounts(): Promise<any> {
  const resp = await request('/api/changelog/filter-counts')
  return jsonOrThrow(resp)
}

export async function listChangelog(filters: Record<string, string> = {}): Promise<any> {
  const params = new URLSearchParams({ size: '200', ...filters })
  const resp = await request(`/api/changelog?${params.toString()}`)
  return jsonOrThrow(resp)
}
