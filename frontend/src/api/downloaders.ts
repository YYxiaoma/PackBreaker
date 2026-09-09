import { apiClient, strongEtag } from './client';

export type DownloaderKind = 'QBITTORRENT' | 'TRANSMISSION';
export type ProbeStatus = 'UNTESTED' | 'OK' | 'FAILED';

export interface PathMapping {
  remote_prefix: string;
  container_prefix: string;
}

export interface DownloaderCredential {
  username?: string;
  password?: string;
  api_key?: string;
}

export interface Downloader {
  id: string;
  name: string;
  type: DownloaderKind;
  base_url: string;
  credential_configured: boolean;
  monitor_rules: Record<string, unknown>;
  path_mappings: PathMapping[];
  capabilities: Record<string, unknown>;
  connection_status: ProbeStatus;
  path_mapping_status: ProbeStatus;
  enabled: boolean;
  version: number;
  last_test_at: string | null;
  last_path_diagnostic_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface DownloaderCreateInput {
  name: string;
  type: DownloaderKind;
  base_url: string;
  credential?: DownloaderCredential;
  monitor_rules: Record<string, unknown>;
  path_mappings: PathMapping[];
}

export interface DownloaderPatchInput {
  name?: string;
  type?: DownloaderKind;
  base_url?: string;
  credential?: DownloaderCredential;
  clear_credential?: boolean;
  monitor_rules?: Record<string, unknown>;
  path_mappings?: PathMapping[];
}

export interface ConnectionProbeResult {
  status: 'ok';
  capabilities: Record<string, unknown>;
}

export interface PathDiagnosticProbeInput {
  remote_path: string;
  target_directory: string;
}

export interface PathDiagnosticResult {
  status: 'ok' | 'blocked';
  rule_index: number;
  container_visible: boolean;
  regular_file: boolean;
  readable: boolean;
  target_writable: boolean;
  round_trip: boolean;
  source_device: number;
  target_device: number;
  same_device: boolean;
  hardlink_feasible: boolean;
  error_code: string | null;
}

export interface PathDiagnosticReport {
  status: 'ok' | 'blocked';
  all_mappings_verified: boolean;
  error_code: string | null;
  results: PathDiagnosticResult[];
}

export async function listDownloaders(): Promise<Downloader[]> {
  const response = await apiClient.get<{ items: Downloader[] }>('/downloaders');
  return response.data.items;
}

export async function getDownloader(id: string): Promise<Downloader> {
  const response = await apiClient.get<Downloader>(`/downloaders/${id}`);
  return response.data;
}

export async function createDownloader(payload: DownloaderCreateInput): Promise<Downloader> {
  const response = await apiClient.post<Downloader>('/downloaders', payload);
  return response.data;
}

export async function updateDownloader(
  id: string,
  version: number,
  payload: DownloaderPatchInput,
): Promise<Downloader> {
  const response = await apiClient.patch<Downloader>(`/downloaders/${id}`, payload, {
    headers: { 'If-Match': strongEtag(version) },
  });
  return response.data;
}

export async function deleteDownloader(id: string, version: number): Promise<void> {
  await apiClient.delete(`/downloaders/${id}`, {
    headers: { 'If-Match': strongEtag(version) },
  });
}

export async function testDownloader(id: string): Promise<ConnectionProbeResult> {
  const response = await apiClient.post<ConnectionProbeResult>(`/downloaders/${id}/test`);
  return response.data;
}

export async function diagnoseDownloaderPaths(
  id: string,
  probes: PathDiagnosticProbeInput[],
): Promise<PathDiagnosticReport> {
  const response = await apiClient.post<PathDiagnosticReport>(
    `/downloaders/${id}/path-diagnostics`,
    {
      probes,
    },
  );
  return response.data;
}

export async function setDownloaderEnabled(
  id: string,
  version: number,
  enabled: boolean,
): Promise<Downloader> {
  const response = await apiClient.post<Downloader>(
    `/downloaders/${id}/actions`,
    { action: enabled ? 'enable' : 'disable' },
    { headers: { 'If-Match': strongEtag(version) } },
  );
  return response.data;
}
