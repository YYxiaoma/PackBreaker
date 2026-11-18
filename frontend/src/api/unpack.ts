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

export interface UnpackTemplate {
  id: string;
  name: string;
  description: string | null;
  current_version: number;
  status: 'IMPORTING' | 'READY' | 'INCOMPLETE' | string;
  expected_seed_count: number;
  seed_count: number;
  mapped_media_count: number;
  site_keys: string[];
  site_ids: string[];
  available_site_ids: string[];
  origin: 'USER' | 'EXPORTED' | 'BUILTIN' | 'COMMUNITY' | string;
  content_digest: string;
  source_provenance: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface UnpackTemplateSeedMapping {
  torrent_id: string;
  collection_root_name: string;
  relative_directory: string;
  media_title: string;
  confidence_bps?: number;
  source_execution_id?: string | null;
}

export interface UnpackTemplateImportRequest {
  name?: string | null;
  description?: string | null;
  template_id?: string | null;
  site_id: string;
  torrent_ids: string[];
  source_label?: string | null;
  seed_mappings?: UnpackTemplateSeedMapping[];
}

export interface UnpackTemplateImportJob {
  id: string;
  template_id: string;
  template_version: number;
  status: 'PENDING' | 'RUNNING' | 'COMPLETED' | 'COMPLETED_WITH_ERRORS' | string;
  total_count: number;
  processed_count: number;
  success_count: number;
  failure_count: number;
  errors: Array<{ torrent_id: string; code: string; message: string }>;
  created_at: string;
  updated_at: string;
}
export type UnpackDefinitionActionResult = components['schemas']['UnpackDefinitionActionResponse'];
export type UnpackExecution = components['schemas']['UnpackExecutionResponse'];
export type UnpackExecutionItem = components['schemas']['UnpackExecutionItemResponse'];

/** Recorded journal evidence only; this endpoint performs no live client or disk query. */
export interface UnpackOperationDiagnostic {
  operation_type: string;
  status: string;
  last_error_code: string | null;
  downloader_id: string | null;
  torrent_hash: string | null;
  target_path: string | null;
  last_observed_state: string | null;
  recorded_file_type: string | null;
  recorded_device: number | null;
  recorded_inode: number | null;
  recorded_size: number | null;
  updated_at: string;
}

export interface UnpackItemDiagnostics {
  item_id: string;
  status: UnpackItemStatus;
  item_version: number;
  execution_stage: string | null;
  plan_status: 'NOT_CREATED' | 'READY' | 'BLOCKED' | 'INVALID';
  plan_blocked_reasons: string[];
  acceptance_read_only: boolean;
  last_error_code: string | null;
  last_error_message: string | null;
  requires_manual_reconciliation: boolean;
  reconciliation_reasons: string[];
  downloader_state_live_checked: boolean;
  filesystem_state_live_checked: boolean;
  operations_truncated: boolean;
  operations: UnpackOperationDiagnostic[];
}

export type UnpackLiveClientRead = components['schemas']['UnpackLiveClientReadResponse'];

export type UnpackExecutionStatus = components['schemas']['UnpackExecutionStatus'];
export type UnpackItemStatus = components['schemas']['UnpackItemStatus'];
export type UnpackMatchCandidate = components['schemas']['UnpackMatchCandidateResponse'];
export type UnpackMatchCandidateList = components['schemas']['UnpackMatchCandidateListResponse'];
export type ManualCandidatePreview = components['schemas']['ManualCandidatePreviewResponse'];
export type ManualCandidateAddResult = components['schemas']['ManualCandidateAddResponse'];

export interface ManualCandidateInput {
  siteId: string;
  generation: number;
  itemVersion: number;
  sourceUrl?: string;
  file?: File;
}

async function manualCandidateRequestBody(
  payload: ManualCandidateInput,
  metainfoDigest?: string,
  payloadSha256?: string,
): Promise<Record<string, string | number>> {
  const body: Record<string, string | number> = {
    site_id: payload.siteId,
    expected_generation: payload.generation,
    expected_item_version: payload.itemVersion,
  };
  if (payload.sourceUrl?.trim()) body.source_url = payload.sourceUrl.trim();
  if (payload.file) {
    const bytes = new Uint8Array(await payload.file.arrayBuffer());
    let binary = '';
    const chunkSize = 0x8000;
    for (let offset = 0; offset < bytes.length; offset += chunkSize) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
    }
    body.torrent_file_base64 = btoa(binary);
    body.torrent_file_name = payload.file.name;
  }
  if (metainfoDigest) body.expected_metainfo_digest = metainfoDigest;
  if (payloadSha256) body.expected_payload_sha256 = payloadSha256;
  return body;
}
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

export async function createUnpackTreeDirectory(
  selectionToken: string,
  name: string,
): Promise<UnpackTreeEntry> {
  const token = selectionToken.trim();
  if (!token) throw new Error('selectionToken 不能为空');
  try {
    const response = await apiClient.post<UnpackTreeEntry>('/files/tree/directories', {
      selection_token: token,
      name,
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

export type UnpackTemplateFilePreview = {
  name: string;
  description: string | null;
  site_key: string;
  seed_count: number;
  mapped_media_count: number;
  resolved_name: string;
  renamed: boolean;
  warnings: string[];
};

export type UnpackTemplateFileImportResult = {
  template_id: string;
  template_version: number;
  name: string;
  site_key: string;
  seed_count: number;
  status: string;
  renamed: boolean;
};

async function postTemplateFile<T>(path: string, file: File): Promise<T> {
  try {
    const response = await apiClient.post<T>(path, file, {
      headers: { 'Content-Type': 'application/octet-stream' },
    });
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function previewUnpackTemplateFile(file: File): Promise<UnpackTemplateFilePreview> {
  return postTemplateFile<UnpackTemplateFilePreview>('/unpack/templates/import-file/preview', file);
}

export async function importUnpackTemplateFile(
  file: File,
): Promise<UnpackTemplateFileImportResult> {
  return postTemplateFile<UnpackTemplateFileImportResult>('/unpack/templates/import-file', file);
}

export type UnpackTemplateExportResult = {
  blob: Blob;
  filename: string;
  seedCount: number;
  skippedCount: number;
};

function filenameFromDisposition(value: string | undefined, fallback: string): string {
  if (!value) return fallback;
  const utf8 = /filename\*=UTF-8''([^;]+)/i.exec(value);
  if (utf8?.[1]) {
    try {
      return decodeURIComponent(utf8[1]);
    } catch {
      return fallback;
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(value);
  return plain?.[1] ?? fallback;
}

export async function exportUnpackExecutionTemplate(
  executionId: string,
  options: { siteKey?: string; collectionRoot?: string } = {},
): Promise<UnpackTemplateExportResult> {
  try {
    const response = await apiClient.post(
      `/unpack/executions/${requiredId(executionId, 'executionId')}/template-export`,
      {},
      {
        params: {
          site_key: options.siteKey || undefined,
          collection_root: options.collectionRoot || undefined,
        },
        responseType: 'blob',
      },
    );
    const headers = response.headers as Record<string, string>;
    return {
      blob: response.data as Blob,
      filename: filenameFromDisposition(
        headers['content-disposition'],
        'packbreaker-template.pbtemplate',
      ),
      seedCount: Number(headers['x-packbreaker-template-seeds'] ?? 0),
      skippedCount: Number(headers['x-packbreaker-template-skipped'] ?? 0),
    };
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function listUnpackTemplates(): Promise<UnpackTemplate[]> {
  try {
    const response = await apiClient.get<{ items: UnpackTemplate[] }>('/unpack/templates');
    return response.data.items;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function createUnpackTemplateImport(
  payload: UnpackTemplateImportRequest,
): Promise<UnpackTemplateImportJob> {
  try {
    const response = await apiClient.post<UnpackTemplateImportJob>(
      '/unpack/templates/import',
      payload,
    );
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function getUnpackTemplateImportJob(jobId: string): Promise<UnpackTemplateImportJob> {
  try {
    const response = await apiClient.get<UnpackTemplateImportJob>(
      `/unpack/templates/import-jobs/${requiredId(jobId, 'jobId')}`,
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

export async function updateUnpackDefinition(
  definitionId: string,
  version: number,
  payload: UnpackDefinitionCreate,
): Promise<UnpackDefinition> {
  try {
    const response = await apiClient.put<UnpackDefinition>(
      `/unpack/definitions/${requiredId(definitionId, 'definitionId')}`,
      payload,
      { headers: { 'If-Match': String(version) } },
    );
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function deleteUnpackDefinition(definitionId: string, version: number): Promise<void> {
  try {
    await apiClient.delete(`/unpack/definitions/${requiredId(definitionId, 'definitionId')}`, {
      headers: { 'If-Match': String(version) },
    });
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function retryUnpackExecutionItems(
  executionId: string,
  version: number,
  itemStatuses: UnpackItemStatus[],
): Promise<{ execution_id: string; retried_count: number; skipped_unsafe_count: number }> {
  try {
    const response = await apiClient.post<{
      execution_id: string;
      retried_count: number;
      skipped_unsafe_count: number;
    }>(
      `/unpack/executions/${requiredId(executionId, 'executionId')}/actions`,
      { action: 'retry_eligible_items', item_statuses: itemStatuses },
      { headers: { 'If-Match': String(version) } },
    );
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

/** Explicit one-shot read-only download-client RPC; no background polling. */
export async function getUnpackItemLiveClientState(itemId: string): Promise<UnpackLiveClientRead> {
  try {
    const response = await apiClient.get<UnpackLiveClientRead>(
      `/unpack/items/${requiredId(itemId, 'itemId')}/diagnostics/live`,
    );
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function getUnpackItemDiagnostics(itemId: string): Promise<UnpackItemDiagnostics> {
  try {
    const response = await apiClient.get<UnpackItemDiagnostics>(
      `/unpack/items/${requiredId(itemId, 'itemId')}/diagnostics`,
    );
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

export async function previewManualUnpackCandidate(
  itemId: string,
  payload: ManualCandidateInput,
): Promise<ManualCandidatePreview> {
  try {
    const response = await apiClient.post<ManualCandidatePreview>(
      `/unpack/items/${requiredId(itemId, 'itemId')}/manual-candidate-preview`,
      await manualCandidateRequestBody(payload),
    );
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function addManualUnpackCandidate(
  itemId: string,
  payload: ManualCandidateInput,
  metainfoDigest: string,
  payloadSha256: string,
): Promise<ManualCandidateAddResult> {
  if (!/^[0-9a-f]{64}$/i.test(metainfoDigest)) throw new Error('种子元信息摘要无效，请重新预览');
  if (!/^[0-9a-f]{64}$/i.test(payloadSha256)) throw new Error('种子文件摘要无效，请重新预览');
  try {
    const response = await apiClient.post<ManualCandidateAddResult>(
      `/unpack/items/${requiredId(itemId, 'itemId')}/manual-candidates`,
      await manualCandidateRequestBody(payload, metainfoDigest, payloadSha256),
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

export async function retryUnpackItemContentVerification(
  itemId: string,
  itemVersion: number,
): Promise<UnpackItemActionResponse> {
  if (!Number.isInteger(itemVersion) || itemVersion < 1) {
    throw new Error('itemVersion 必须是正整数');
  }
  try {
    const response = await apiClient.post<UnpackItemActionResponse>(
      `/unpack/items/${requiredId(itemId, 'itemId')}/actions`,
      { action: 'retry_content_verification' },
      { headers: { 'If-Match': String(itemVersion) } },
    );
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export type UnpackExecutionReconcileResult = {
  execution_id: string;
  processed_count: number;
  resolved_count: number;
  manual_count: number;
  failed_count: number;
  failed_reasons: string[];
};

export async function reconcileUnpackExecution(
  executionId: string,
): Promise<UnpackExecutionReconcileResult> {
  try {
    const response = await apiClient.post<UnpackExecutionReconcileResult>(
      `/unpack/executions/${requiredId(executionId, 'executionId')}/reconcile`,
    );
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export type UnpackItemReconcileResult = {
  item_id: string;
  item_status: string;
  resumed_stage: string | null;
  resolved_count: number;
  manual_count: number;
  outcomes: {
    journal_id: string;
    operation_type: string;
    resolution: 'APPLIED' | 'NO_EFFECT' | 'MANUAL' | 'PLAN_RESET';
    note: string;
  }[];
};

export async function reconcileUnpackExecutionItem(
  itemId: string,
  itemVersion: number,
): Promise<UnpackItemReconcileResult> {
  if (!Number.isInteger(itemVersion) || itemVersion < 1) {
    throw new Error('itemVersion 必须是正整数');
  }
  try {
    const response = await apiClient.post<UnpackItemReconcileResult>(
      `/unpack/items/${requiredId(itemId, 'itemId')}/reconcile`,
      {},
      { headers: { 'If-Match': String(itemVersion) } },
    );
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function deleteUnpackExecutionItem(
  itemId: string,
  itemVersion: number,
): Promise<void> {
  if (!Number.isInteger(itemVersion) || itemVersion < 1) {
    throw new Error('itemVersion 必须是正整数');
  }
  try {
    await apiClient.delete(`/unpack/items/${requiredId(itemId, 'itemId')}`, {
      headers: { 'If-Match': String(itemVersion) },
    });
  } catch (error) {
    throw toApiProblem(error);
  }
}
