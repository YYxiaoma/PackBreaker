import { apiClient, strongEtag } from './client';
import type { components } from './generated/schema';

export type SystemHealth = components['schemas']['SystemHealthResponse'];
export type SystemHealthCheck = components['schemas']['SystemHealthCheckResponse'];
export type OperationalLogEntry = components['schemas']['OperationalLogEntryResponse'];
export type OperationalLogList = components['schemas']['OperationalLogListResponse'];
export type OperationalLogLevel = OperationalLogEntry['level'];
export type BackupPolicy = components['schemas']['BackupPolicyResponse'];
export type BackupPolicyUpdate = components['schemas']['BackupPolicyUpdateRequest'];
export type BackupRun = components['schemas']['BackupRunResponse'];
export type ReleasePreflight = components['schemas']['ReleasePreflightResponse'];
export type ReleasePreflightCheck = components['schemas']['ReleasePreflightCheckResponse'];

export interface OperationalLogQuery {
  window_minutes?: number;
  limit?: number;
  level?: OperationalLogLevel;
  q?: string;
}

export interface DownloadArtifact {
  blob: Blob;
  filename: string;
}

function attachmentFilename(contentDisposition: unknown, fallback: string): string {
  if (typeof contentDisposition !== 'string') return fallback;
  const match = /filename="([^"]+)"/.exec(contentDisposition);
  return match?.[1] || fallback;
}

export async function getReleasePreflight(): Promise<ReleasePreflight> {
  const response = await apiClient.get<ReleasePreflight>('/system/release/preflight');
  return response.data;
}

export async function getBackupPolicy(): Promise<BackupPolicy> {
  const response = await apiClient.get<BackupPolicy>('/system/backups/policy');
  return response.data;
}

export async function updateBackupPolicy(
  version: number,
  payload: BackupPolicyUpdate,
): Promise<BackupPolicy> {
  const response = await apiClient.put<BackupPolicy>('/system/backups/policy', payload, {
    headers: { 'If-Match': strongEtag(version) },
  });
  return response.data;
}

export async function runBackupNow(): Promise<BackupRun> {
  const response = await apiClient.post<BackupRun>('/system/backups/actions', {
    action: 'run_now',
  });
  return response.data;
}

export async function getSystemHealth(): Promise<SystemHealth> {
  const response = await apiClient.get<SystemHealth>('/system/health');
  return response.data;
}

export async function listSystemLogs(query: OperationalLogQuery = {}): Promise<OperationalLogList> {
  const response = await apiClient.get<OperationalLogList>('/system/logs', { params: query });
  return response.data;
}

export async function exportSystemDiagnostics(): Promise<DownloadArtifact> {
  const response = await apiClient.get<Blob>('/system/diagnostics/export', {
    responseType: 'blob',
  });
  return {
    blob: response.data,
    filename: attachmentFilename(
      response.headers['content-disposition'],
      'packbreaker-diagnostics.zip',
    ),
  };
}

export async function exportSystemLogs(query: OperationalLogQuery = {}): Promise<DownloadArtifact> {
  const response = await apiClient.get<Blob>('/system/logs/export', {
    params: query,
    responseType: 'blob',
  });
  return {
    blob: response.data,
    filename: attachmentFilename(response.headers['content-disposition'], 'packbreaker-logs.json'),
  };
}
