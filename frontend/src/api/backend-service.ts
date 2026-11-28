import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export async function fetchSnapshot(key: string): Promise<Record<string, any>> {
  const response = await request(`/api/snapshot/${key}`)
  if (!response.ok) throw new Error('跨模块快照读取失败')
  return await response.json()
}

/** 凭确认回执核收缺件分段（同一回执后端只记一次）。 */
export async function registerMissingReceipt(
  itemId: number,
  ackNo: string,
  segments: string[],
): Promise<ActionResult> {
  const response = await request(`/api/missing_item/${itemId}/receipts`, {
    method: 'POST',
    body: JSON.stringify({ ack_no: ackNo, segments }),
  })
  if (!response.ok) {
    const detail = (await response.json().catch(() => ({}))) as { detail?: string }
    throw new Error(detail.detail ?? '回执核收失败')
  }
  return await response.json()
}

/** 补传工单取得确认回执后核收。 */
export async function attachResendReceipt(
  orderId: number,
  ackNo: string,
  segments: string[],
): Promise<ActionResult> {
  const response = await request(`/api/resend_order/${orderId}/receipts`, {
    method: 'POST',
    body: JSON.stringify({ ack_no: ackNo, segments }),
  })
  if (!response.ok) {
    const detail = (await response.json().catch(() => ({}))) as { detail?: string }
    throw new Error(detail.detail ?? '回执核收失败')
  }
  return await response.json()
}
