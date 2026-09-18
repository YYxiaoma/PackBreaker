import { apiClient } from './client';

export type TaskDefinitionKind = 'MANUAL' | 'MONITOR';
export type TaskDefinitionStatus = 'ENABLED' | 'PAUSED' | 'SITE_UNAVAILABLE' | 'ERROR';
export type TaskSourceKind = 'DOWNLOADER' | 'DIRECTORY';
export type TaskStorageMode = 'HARDLINK' | 'SYMLINK' | 'COPY';
export type TaskConflictPolicy = 'VERIFY_REUSE_OR_STOP' | 'SKIP' | 'RENAME' | 'OVERWRITE';
export type TaskInitialScope = 'NEW_ONLY' | 'INCLUDE_EXISTING';
export type TaskOverlapPolicy = 'SKIP' | 'RUN_ONCE_AFTER';

export interface TaskExecutionSummary {
  id: string;
  status: string;
  phase: string;
  trigger: string;
  success_count: number;
  failed_count: number;
  skipped_count: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface TaskExecutionItem {
  id: string;
  unpack_task_id: string | null;
  source_object_key: string;
  name: string;
  source: string;
  size_bytes: number | null;
  phase: string;
  progress: number | null;
  result: string | null;
  error_code: string | null;
  error_summary_zh: string | null;
  technical_detail: string | null;
  retryable: boolean;
  retry_count: number;
  lifecycle_stage: string;
  risk_level: string;
  authorization_status: string;
  execution_plan_id: string | null;
  execution_plan_ready: boolean | null;
  side_effects_started: boolean;
  lifecycle_blocked_reasons: string[];
  risk_summary: TaskRiskSummary | null;
  approval: TaskApproval | null;
  closure: TaskExecutionClosure;
}

export interface TaskExecutionClosure {
  status: string;
  filesystem_status: string;
  downloader_status: string;
  operation_attention_count: number;
  reconcile_required_count: number;
  rollback_blocked_count: number;
  retention_candidate_count: number;
  manual_attention_required: boolean;
  issue_codes: string[];
}

export interface TaskRiskSummary {
  id: string;
  execution_plan_id: string;
  plan_digest: string;
  risk_level: string;
  reason_codes: string[];
  action_kinds: string[];
  hardlink_count: number;
  client_fetch_count: number;
  create_directory_count: number;
  estimated_download_bytes_upper_bound: number;
  risk_digest: string;
  created_at: string;
}

export interface TaskApproval {
  id: string;
  execution_plan_id: string;
  plan_digest: string;
  state: string;
  decision_source: string | null;
  actor_kind: string | null;
  actor_id: string | null;
  decision_note: string | null;
  decided_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface TaskExecutionEvent {
  id: string;
  event_code: string;
  message: string;
  trace_id: string;
  context: Record<string, unknown>;
  created_at: string;
}

export interface TaskExecution extends TaskExecutionSummary {
  task_definition_id: string | null;
  task_name: string;
  source_execution_id: string | null;
  discovered_count: number;
  trace_id: string;
  config_snapshot: Record<string, unknown>;
  items: TaskExecutionItem[];
  events: TaskExecutionEvent[];
}

export interface TaskExecutionListItem extends TaskExecutionSummary {
  task_definition_id: string | null;
  task_name: string;
  source_execution_id: string | null;
  discovered_count: number;
  trace_id: string;
}

export interface TaskExecutionPage {
  items: TaskExecutionListItem[];
  page: number;
  page_size: number;
  total: number;
}

export interface TaskExecutionQuery {
  page?: number;
  page_size?: number;
  status?: string;
  trigger?: string;
  search?: string;
  started_from?: string;
  started_to?: string;
}

export interface TaskMonitorScan {
  task_definition_id: string;
  trigger: string;
  outcome: string;
  discovered_count: number;
  new_count: number;
  next_run_at: string | null;
  execution: TaskExecution | null;
}

export interface TaskDefinition {
  id: string;
  name: string;
  kind: TaskDefinitionKind;
  status: TaskDefinitionStatus;
  site_id: string | null;
  site_name: string | null;
  site_available: boolean;
  source_kind: TaskSourceKind;
  source_downloader_id: string | null;
  source_downloader_name: string | null;
  source_directory: string | null;
  source_available: boolean;
  source_config: Record<string, unknown>;
  cron_expression: string | null;
  timezone: string | null;
  last_scan_at: string | null;
  last_successful_scan_at: string | null;
  next_run_at: string | null;
  file_types: string[];
  video_extensions: string[];
  archive_extensions: string[];
  min_size_bytes: number | null;
  max_size_bytes: number | null;
  include_name: string | null;
  exclude_names: string[];
  ignore_temp_files: boolean;
  temp_patterns: string[];
  include_subdirectories: boolean;
  max_scan_depth: number | null;
  output_directory: string;
  storage_mode: TaskStorageMode;
  preserve_structure: boolean;
  conflict_policy: TaskConflictPolicy;
  stability_detection_enabled: boolean;
  stability_wait_seconds: number;
  only_completed_downloads: boolean;
  initial_scope: TaskInitialScope;
  debounce_seconds: number;
  overlap_policy: TaskOverlapPolicy;
  auto_retry_enabled: boolean;
  max_auto_retries: number;
  retry_intervals_seconds: number[];
  high_risk_preauthorization_enabled: boolean;
  high_risk_allowed_action_kinds: string[];
  latest_execution: TaskExecutionSummary | null;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface TaskDirectoryEntry {
  name: string;
  path: string;
}

export interface TaskDirectoryBrowse {
  current_path: string;
  entries: TaskDirectoryEntry[];
}

export interface TaskDirectoryFile {
  relative_path: string;
  size_bytes: number;
  device: number;
  inode: number;
  mtime_ns: number;
}

export interface TaskDirectoryPreview {
  directory_path: string;
  matched_count: number;
  total_size_bytes: number;
  files: TaskDirectoryFile[];
}

export interface TaskCronPreview {
  cron_expression: string;
  timezone: string;
  description: string;
  next_runs: string[];
}

export type TaskDefinitionPrecheckStatus = 'OK' | 'WARNING' | 'BLOCKED';

export interface TaskDefinitionPrecheckItem {
  code: string;
  status: TaskDefinitionPrecheckStatus;
  title: string;
  detail: string;
}

export interface TaskDefinitionPrecheck {
  status: TaskDefinitionPrecheckStatus;
  items: TaskDefinitionPrecheckItem[];
}

export interface TaskFilterInput {
  file_types: string[];
  video_extensions: string[];
  archive_extensions: string[];
  min_size_bytes: number | null;
  max_size_bytes: number | null;
  include_name: string | null;
  exclude_names: string[];
  ignore_temp_files: boolean;
  temp_patterns: string[];
  include_subdirectories: boolean;
  max_scan_depth: number | null;
}

export interface TaskDefinitionCreateInput {
  name: string;
  kind: TaskDefinitionKind;
  site_id: string;
  source: {
    kind: TaskSourceKind;
    downloader_id?: string;
    directory_path?: string;
    config?: Record<string, unknown>;
  };
  cron_expression?: string;
  filters: TaskFilterInput;
  output_policy: {
    output_directory: string;
    storage_mode: TaskStorageMode;
    preserve_structure: boolean;
    conflict_policy: TaskConflictPolicy;
  };
  execution_policy: {
    stability_detection_enabled: boolean;
    stability_wait_seconds: number;
    only_completed_downloads: boolean;
    initial_scope: TaskInitialScope;
    debounce_seconds: number;
    overlap_policy: TaskOverlapPolicy;
    auto_retry_enabled: boolean;
    max_auto_retries: number;
    retry_intervals_seconds: number[];
    high_risk_preauthorization_enabled: boolean;
    high_risk_allowed_action_kinds: string[];
  };
}

export async function browseTaskDirectories(path = '.'): Promise<TaskDirectoryBrowse> {
  const response = await apiClient.get<TaskDirectoryBrowse>(
    '/task-definitions/source-directories',
    {
      params: { path },
    },
  );
  return response.data;
}

export async function previewTaskDirectory(
  directoryPath: string,
  filters: TaskFilterInput,
): Promise<TaskDirectoryPreview> {
  const response = await apiClient.post<TaskDirectoryPreview>(
    '/task-definitions/directory-preview',
    {
      directory_path: directoryPath,
      filters,
    },
  );
  return response.data;
}

export async function previewTaskCron(cronExpression: string): Promise<TaskCronPreview> {
  const response = await apiClient.post<TaskCronPreview>('/task-definitions/cron-preview', {
    cron_expression: cronExpression,
  });
  return response.data;
}

export async function precheckTaskDefinition(
  payload: TaskDefinitionCreateInput,
): Promise<TaskDefinitionPrecheck> {
  const response = await apiClient.post<TaskDefinitionPrecheck>(
    '/task-definitions/precheck',
    payload,
  );
  return response.data;
}

export async function listTaskDefinitions(kind?: TaskDefinitionKind): Promise<TaskDefinition[]> {
  const response = await apiClient.get<{ items: TaskDefinition[] }>('/task-definitions', {
    params: kind ? { kind } : undefined,
  });
  return response.data.items;
}

export async function createTaskDefinition(
  payload: TaskDefinitionCreateInput,
): Promise<TaskDefinition> {
  const response = await apiClient.post<TaskDefinition>('/task-definitions', payload);
  return response.data;
}

export async function updateTaskDefinition(
  id: string,
  payload: TaskDefinitionCreateInput,
): Promise<TaskDefinition> {
  const response = await apiClient.put<TaskDefinition>(`/task-definitions/${id}`, payload);
  return response.data;
}

export async function deleteTaskDefinition(id: string): Promise<void> {
  await apiClient.delete(`/task-definitions/${id}`);
}

export async function executeManualTaskDefinition(id: string): Promise<TaskExecution> {
  const response = await apiClient.post<TaskExecution>(`/task-definitions/${id}/executions`);
  return response.data;
}

export async function scanMonitorTaskDefinition(id: string): Promise<TaskMonitorScan> {
  const response = await apiClient.post<TaskMonitorScan>(`/task-definitions/${id}/scan`);
  return response.data;
}

export async function setTaskDefinitionPaused(
  id: string,
  paused: boolean,
): Promise<TaskDefinition> {
  const response = await apiClient.post<TaskDefinition>(`/task-definitions/${id}/actions`, {
    action: paused ? 'pause' : 'resume',
  });
  return response.data;
}

export async function getTaskDefinitionExecution(
  definitionId: string,
  executionId: string,
): Promise<TaskExecution> {
  const response = await apiClient.get<TaskExecution>(
    `/task-definitions/${definitionId}/executions/${executionId}`,
  );
  return response.data;
}

export async function advanceTaskDefinitionExecution(
  definitionId: string,
  executionId: string,
  idempotencyKey: string,
): Promise<TaskExecution> {
  const response = await apiClient.post<TaskExecution>(
    `/task-definitions/${definitionId}/executions/${executionId}/advance`,
    undefined,
    { headers: { 'Idempotency-Key': idempotencyKey } },
  );
  return response.data;
}

export async function decideTaskDefinitionExecutionApproval(
  definitionId: string,
  executionId: string,
  itemId: string,
  payload: {
    execution_plan_id: string;
    decision: 'APPROVE' | 'REJECT';
    note?: string;
  },
  idempotencyKey: string,
): Promise<TaskExecution> {
  const response = await apiClient.post<TaskExecution>(
    `/task-definitions/${definitionId}/executions/${executionId}/items/${itemId}/approval`,
    payload,
    { headers: { 'Idempotency-Key': idempotencyKey } },
  );
  return response.data;
}

export async function listTaskDefinitionExecutions(
  definitionId: string,
  query: TaskExecutionQuery = {},
): Promise<TaskExecutionPage> {
  const response = await apiClient.get<TaskExecutionPage>(
    `/task-definitions/${definitionId}/executions`,
    { params: query },
  );
  return response.data;
}

export async function retryFailedTaskDefinitionExecution(
  definitionId: string,
  executionId: string,
  idempotencyKey: string,
): Promise<TaskExecution> {
  const response = await apiClient.post<TaskExecution>(
    `/task-definitions/${definitionId}/executions/${executionId}/retry-failed`,
    undefined,
    { headers: { 'Idempotency-Key': idempotencyKey } },
  );
  return response.data;
}
