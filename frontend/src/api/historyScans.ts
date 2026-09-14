import { apiClient, strongEtag, toApiProblem } from './client';
import type { components } from './generated/schema';

export type HistoryScan = components['schemas']['HistoryScanResponse'];
export type HistoryScanCreateInput = components['schemas']['HistoryScanCreateRequest'];
export type HistoryScanActionInput = components['schemas']['HistoryScanActionRequest'];
export type HistoryScanBatch = components['schemas']['HistoryScanBatchResponse'];

function historyScanPath(scanId: string): string {
  const normalized = scanId.trim();
  if (!normalized) throw new Error('scanId 不能为空');
  return `/history-scans/${encodeURIComponent(normalized)}`;
}

export async function listHistoryScans(): Promise<HistoryScan[]> {
  try {
    const response = await apiClient.get<{ items: HistoryScan[] }>('/history-scans');
    return response.data.items;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function createHistoryScan(payload: HistoryScanCreateInput): Promise<HistoryScan> {
  try {
    const response = await apiClient.post<HistoryScan>('/history-scans', payload);
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function updateHistoryScan(
  scanId: string,
  version: number,
  payload: HistoryScanActionInput,
): Promise<HistoryScan | HistoryScanBatch> {
  try {
    const response = await apiClient.post<HistoryScan | HistoryScanBatch>(
      `${historyScanPath(scanId)}/actions`,
      payload,
      { headers: { 'If-Match': strongEtag(version) } },
    );
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function startHistoryScan(scanId: string, version: number): Promise<HistoryScan> {
  return (await updateHistoryScan(scanId, version, { action: 'start', limit: 100 })) as HistoryScan;
}

export async function pauseHistoryScan(scanId: string, version: number): Promise<HistoryScan> {
  return (await updateHistoryScan(scanId, version, { action: 'pause', limit: 100 })) as HistoryScan;
}

export async function resumeHistoryScan(scanId: string, version: number): Promise<HistoryScan> {
  return (await updateHistoryScan(scanId, version, {
    action: 'resume',
    limit: 100,
  })) as HistoryScan;
}

export async function scanHistoryBatch(
  scanId: string,
  version: number,
  limit = 100,
): Promise<HistoryScanBatch> {
  return (await updateHistoryScan(scanId, version, { action: 'scan', limit })) as HistoryScanBatch;
}
