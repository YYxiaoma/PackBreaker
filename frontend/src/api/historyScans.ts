import { apiClient, strongEtag, toApiProblem } from './client';
import type { components } from './generated/schema';

export type HistoryScan = components['schemas']['HistoryScanResponse'];
export type HistoryScanCreateInput = components['schemas']['HistoryScanCreateRequest'];
export type HistoryScanActionInput = components['schemas']['HistoryScanActionRequest'];
export type HistoryScanBatch = components['schemas']['HistoryScanBatchResponse'];
export type HistoryScanMaterialize = components['schemas']['HistoryScanMaterializeResponse'];
export type HistoryTaskResult = components['schemas']['HistoryTaskResultResponse'];
export type HistoryTaskBatchActionInput = components['schemas']['HistoryTaskBatchActionRequest'];
export type HistoryTaskBatchAnalyze = components['schemas']['HistoryTaskBatchAnalyzeResponse'];

export interface HistoryTaskFilters {
  taskStatus?: NonNullable<HistoryTaskResult['task_status']>;
  materializationStatus?: HistoryTaskResult['materialization_status'];
  query?: string;
}

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

export async function listHistoryScanTasks(
  scanId: string,
  limit = 500,
  filters: HistoryTaskFilters = {},
): Promise<HistoryTaskResult[]> {
  if (!Number.isInteger(limit) || limit < 1 || limit > 1000) {
    throw new Error('limit 必须位于 1..1000');
  }
  const query = filters.query?.trim();
  try {
    const response = await apiClient.get<{ items: HistoryTaskResult[] }>(
      `${historyScanPath(scanId)}/tasks`,
      {
        params: {
          limit,
          ...(filters.taskStatus ? { task_status: filters.taskStatus } : {}),
          ...(filters.materializationStatus
            ? { materialization_status: filters.materializationStatus }
            : {}),
          ...(query ? { query } : {}),
        },
      },
    );
    return response.data.items;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function analyzeHistoryScanTasks(
  scanId: string,
  taskIds: string[],
): Promise<HistoryTaskBatchAnalyze> {
  const normalized = [...new Set(taskIds.map((item) => item.trim()).filter(Boolean))];
  if (normalized.length < 1 || normalized.length > 10) {
    throw new Error('批量分析必须选择 1 到 10 个历史任务');
  }
  const payload: HistoryTaskBatchActionInput = { action: 'analyze', task_ids: normalized };
  try {
    const response = await apiClient.post<HistoryTaskBatchAnalyze>(
      `${historyScanPath(scanId)}/tasks/actions`,
      payload,
    );
    return response.data;
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
): Promise<HistoryScan | HistoryScanBatch | HistoryScanMaterialize> {
  try {
    const response = await apiClient.post<HistoryScan | HistoryScanBatch | HistoryScanMaterialize>(
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

export async function cancelHistoryScan(scanId: string, version: number): Promise<HistoryScan> {
  return (await updateHistoryScan(scanId, version, {
    action: 'cancel',
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

export async function materializeHistoryScan(
  scanId: string,
  version: number,
  limit = 100,
): Promise<HistoryScanMaterialize> {
  return (await updateHistoryScan(scanId, version, {
    action: 'materialize',
    limit,
  })) as HistoryScanMaterialize;
}
