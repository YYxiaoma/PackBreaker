import { apiClient, toApiProblem } from './client';
import type { components } from './generated/schema';

export type UnpackTreeEntry = components['schemas']['UnpackTreeEntryResponse'];
export type UnpackTree = components['schemas']['UnpackTreeResponse'];
export type UnpackSourceScanCreate = components['schemas']['UnpackSourceScanCreateRequest'];
export type UnpackSourceScan = components['schemas']['UnpackSourceScanResponse'];
export type UnpackSourceScanItem = components['schemas']['UnpackSourceScanItemResponse'];
export type UnpackSourceScanSelection = components['schemas']['UnpackSourceScanSelectionRequest'];
export type UnpackSourceScanSelectionSummary =
  components['schemas']['UnpackSourceScanSelectionSummaryResponse'];
export type UnpackDefinitionCreate = components['schemas']['UnpackDefinitionCreateRequest'];
export type UnpackDefinition = components['schemas']['UnpackDefinitionResponse'];
export type UnpackDefinitionActionResult = components['schemas']['UnpackDefinitionActionResponse'];
export type UnpackExecution = components['schemas']['UnpackExecutionResponse'];
export type UnpackExecutionItem = components['schemas']['UnpackExecutionItemResponse'];
export type UnpackExecutionStatus = components['schemas']['UnpackExecutionStatus'];
export type UnpackItemStatus = components['schemas']['UnpackItemStatus'];
export type UnpackMatchCandidate = components['schemas']['UnpackMatchCandidateResponse'];
export type UnpackMatchCandidateList = components['schemas']['UnpackMatchCandidateListResponse'];
export type UnpackReviewRequest = components['schemas']['UnpackReviewRequest'];
export type UnpackReviewResponse = components['schemas']['UnpackReviewResponse'];
export type UnpackItemActionResponse = components['schemas']['UnpackItemActionResponse'];

function requiredId(value: string, label: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(`${label} 不能为空`);
  return encodeURIComponent(normalized);
}

export async function listUnpackTreeRoots(): Promise<UnpackTreeEntry[]> {
  try {
    const response = await apiClient.get<{ items: UnpackTreeEntry[] }>('/files/tree/roots');
    return response.data.items;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function browseUnpackTree(selectionToken: string): Promise<UnpackTree> {
  const token = selectionToken.trim();
  if (!token) throw new Error('selectionToken 不能为空');
  try {
    const response = await apiClient.get<UnpackTree>('/files/tree', {
      params: { selection_token: token },
    });
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function createUnpackSourceScan(
  payload: UnpackSourceScanCreate,
): Promise<UnpackSourceScan> {
  try {
    const response = await apiClient.post<UnpackSourceScan>('/unpack/source-scans', payload);
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function getUnpackSourceScan(scanId: string): Promise<UnpackSourceScan> {
  try {
    const response = await apiClient.get<UnpackSourceScan>(
      `/unpack/source-scans/${requiredId(scanId, 'scanId')}`,
    );
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function listUnpackSourceScanItems(
  scanId: string,
  options: {
    cursor?: string;
    limit?: number;
    q?: string;
    extension?: string;
    resolution?: string;
    selected?: boolean;
  } = {},
): Promise<{ items: UnpackSourceScanItem[]; next_cursor: string | null; has_more: boolean }> {
  try {
    const response = await apiClient.get<{
      items: UnpackSourceScanItem[];
      next_cursor: string | null;
      has_more: boolean;
    }>(`/unpack/source-scans/${requiredId(scanId, 'scanId')}/items`, {
      params: {
        ...(options.cursor ? { cursor: options.cursor } : {}),
        ...(options.limit ? { limit: options.limit } : {}),
        ...(options.q ? { q: options.q } : {}),
        ...(options.extension ? { extension: options.extension } : {}),
        ...(options.resolution ? { resolution: options.resolution } : {}),
        ...(options.selected === undefined ? {} : { selected: options.selected }),
      },
    });
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function updateUnpackSourceScanSelection(
  scanId: string,
  payload: UnpackSourceScanSelection,
): Promise<UnpackSourceScanSelectionSummary> {
  try {
    const response = await apiClient.put<UnpackSourceScanSelectionSummary>(
      `/unpack/source-scans/${requiredId(scanId, 'scanId')}/selection`,
      payload,
    );
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function getUnpackSourceScanSelectionSummary(
  scanId: string,
): Promise<UnpackSourceScanSelectionSummary> {
  try {
    const response = await apiClient.get<UnpackSourceScanSelectionSummary>(
      `/unpack/source-scans/${requiredId(scanId, 'scanId')}/selection-summary`,
    );
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function createUnpackDefinition(
  payload: UnpackDefinitionCreate,
): Promise<UnpackDefinition> {
  try {
    const response = await apiClient.post<UnpackDefinition>('/unpack/definitions', payload);
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function listUnpackDefinitions(): Promise<UnpackDefinition[]> {
  try {
    const response = await apiClient.get<{ items: UnpackDefinition[] }>('/unpack/definitions');
    return response.data.items;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function getUnpackDefinition(definitionId: string): Promise<UnpackDefinition> {
  try {
    const response = await apiClient.get<UnpackDefinition>(
      `/unpack/definitions/${requiredId(definitionId, 'definitionId')}`,
    );
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function runUnpackDefinition(
  definitionId: string,
): Promise<UnpackDefinitionActionResult> {
  try {
    const response = await apiClient.post<UnpackDefinitionActionResult>(
      `/unpack/definitions/${requiredId(definitionId, 'definitionId')}/actions`,
      { action: 'run' },
    );
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function listUnpackExecutions(
  options: {
    definitionId?: string;
    status?: UnpackExecutionStatus;
    limit?: number;
  } = {},
): Promise<UnpackExecution[]> {
  try {
    const response = await apiClient.get<{ items: UnpackExecution[] }>('/unpack/executions', {
      params: {
        ...(options.definitionId ? { definition_id: options.definitionId } : {}),
        ...(options.status ? { execution_status: options.status } : {}),
        ...(options.limit ? { limit: options.limit } : {}),
      },
    });
    return response.data.items;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function getUnpackExecution(executionId: string): Promise<UnpackExecution> {
  try {
    const response = await apiClient.get<UnpackExecution>(
      `/unpack/executions/${requiredId(executionId, 'executionId')}`,
    );
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function listUnpackExecutionItems(
  executionId: string,
  options: {
    cursor?: string;
    limit?: number;
    status?: UnpackItemStatus;
    q?: string;
  } = {},
): Promise<{ items: UnpackExecutionItem[]; next_cursor: string | null; has_more: boolean }> {
  try {
    const response = await apiClient.get<{
      items: UnpackExecutionItem[];
      next_cursor: string | null;
      has_more: boolean;
    }>(`/unpack/executions/${requiredId(executionId, 'executionId')}/items`, {
      params: {
        ...(options.cursor ? { cursor: options.cursor } : {}),
        ...(options.limit ? { limit: options.limit } : {}),
        ...(options.status ? { item_status: options.status } : {}),
        ...(options.q ? { q: options.q } : {}),
      },
    });
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function listUnpackItemCandidates(itemId: string): Promise<UnpackMatchCandidateList> {
  try {
    const response = await apiClient.get<UnpackMatchCandidateList>(
      `/unpack/items/${requiredId(itemId, 'itemId')}/candidates`,
    );
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function reviewUnpackItem(
  itemId: string,
  payload: UnpackReviewRequest,
  options: { itemVersion: number; idempotencyKey: string },
): Promise<UnpackReviewResponse> {
  const idempotencyKey = options.idempotencyKey.trim();
  if (!idempotencyKey) throw new Error('Idempotency-Key 不能为空');
  if (!Number.isInteger(options.itemVersion) || options.itemVersion < 1) {
    throw new Error('itemVersion 必须是正整数');
  }
  try {
    const response = await apiClient.put<UnpackReviewResponse>(
      `/unpack/items/${requiredId(itemId, 'itemId')}/review`,
      payload,
      {
        headers: {
          'If-Match': String(options.itemVersion),
          'Idempotency-Key': idempotencyKey,
        },
      },
    );
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function retryUnpackItemMatch(
  itemId: string,
  itemVersion: number,
): Promise<UnpackItemActionResponse> {
  if (!Number.isInteger(itemVersion) || itemVersion < 1) {
    throw new Error('itemVersion 必须是正整数');
  }
  try {
    const response = await apiClient.post<UnpackItemActionResponse>(
      `/unpack/items/${requiredId(itemId, 'itemId')}/actions`,
      { action: 'retry_match' },
      { headers: { 'If-Match': String(itemVersion) } },
    );
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}
