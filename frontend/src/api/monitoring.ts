import { apiClient } from './client';
import type { components } from './generated/schema';

export type SeedingHealthSummary = components['schemas']['SeedingHealthSummaryResponse'];
export type SeedingHealthRun = components['schemas']['SeedingRunResponse'];
export type SeedingIssueRow = components['schemas']['SeedingIssueRowResponse'];
export type SeedingIssueList = components['schemas']['SeedingIssueListResponse'];
export type SeedingIssueDetail = components['schemas']['SeedingIssueDetailResponse'];
export type SeedingPolicy = components['schemas']['SeedingPolicyResponse'];
export type SeedingRepairPreview = {
  action: string;
  site_key: string;
  remote_torrent_id: string;
  downloader_id: string;
  downloader_name: string;
  save_path: string;
  torrent_name: string;
  size_bytes: number;
  file_count: number;
  metainfo_digest: string;
  payload_sha256: string;
  warning: string;
};
export type SeedingRepairResult = {
  journal_id: string;
  status: string;
  torrent_hash: string | null;
  already_recorded: boolean;
};
export type SeedingHealthSettings = components['schemas']['SeedingHealthSettingsResponse'];
export type SeedingIssueAction = 'ACK' | 'IGNORE' | 'RECHECK' | 'RETRIEVE_TORRENT' | 'RESEED';

export interface SeedingIssueQuery {
  kind?: string;
  status?: string;
  severity?: string;
  siteKey?: string;
  downloaderId?: string;
  search?: string;
  page?: number;
  pageSize?: number;
}

const BASE = '/monitor/seeding-health';

export async function getSeedingHealthSummary(): Promise<SeedingHealthSummary> {
  const response = await apiClient.get<SeedingHealthSummary>(`${BASE}/summary`);
  return response.data;
}

export async function listSeedingIssues(query: SeedingIssueQuery = {}): Promise<SeedingIssueList> {
  const response = await apiClient.get<SeedingIssueList>(`${BASE}/issues`, {
    params: {
      kind: query.kind,
      status: query.status,
      severity: query.severity,
      site_key: query.siteKey,
      downloader_id: query.downloaderId,
      q: query.search,
      page: query.page ?? 1,
      page_size: query.pageSize ?? 20,
    },
  });
  return response.data;
}

export async function getSeedingIssue(issueId: string): Promise<SeedingIssueDetail> {
  const response = await apiClient.get<SeedingIssueDetail>(
    `${BASE}/issues/${encodeURIComponent(issueId)}`,
  );
  return response.data;
}

export async function actOnSeedingIssue(
  issueId: string,
  action: SeedingIssueAction,
): Promise<{ id: string; status: string }> {
  const response = await apiClient.post<{ id: string; status: string }>(
    `${BASE}/issues/${encodeURIComponent(issueId)}/actions`,
    { action },
  );
  return response.data;
}

export async function triggerSeedingHealthRun(): Promise<{
  run_id: string | null;
  accepted: boolean;
}> {
  const response = await apiClient.post<{ run_id: string | null; accepted: boolean }>(
    `${BASE}/runs`,
    {},
  );
  return response.data;
}

export async function listSeedingRuns(limit = 10): Promise<SeedingHealthRun[]> {
  const response = await apiClient.get<SeedingHealthRun[]>(`${BASE}/runs`, { params: { limit } });
  return response.data;
}

export async function getSeedingHealthSettings(): Promise<SeedingHealthSettings> {
  const response = await apiClient.get<SeedingHealthSettings>(`${BASE}/settings`);
  return response.data;
}

export async function setSeedingHealthSettings(
  siteProbeEnabled: boolean,
): Promise<SeedingHealthSettings> {
  const response = await apiClient.put<SeedingHealthSettings>(`${BASE}/settings`, {
    site_probe_enabled: siteProbeEnabled,
  });
  return response.data;
}

export async function previewSeedingRepair(
  issueId: string,
  action: string,
): Promise<SeedingRepairPreview> {
  const response = await apiClient.post<SeedingRepairPreview>(
    `${BASE}/issues/${issueId}/repair-preview`,
    { action },
  );
  return response.data;
}

export async function executeSeedingRepair(
  issueId: string,
  payload: {
    action: string;
    expected_payload_sha256: string;
    downloader_id?: string;
    save_path?: string;
  },
): Promise<SeedingRepairResult> {
  const response = await apiClient.post<SeedingRepairResult>(
    `${BASE}/issues/${issueId}/repair`,
    payload,
  );
  return response.data;
}

export async function listSeedingPolicies(): Promise<SeedingPolicy[]> {
  const response = await apiClient.get<SeedingPolicy[]>(`${BASE}/policies`);
  return response.data;
}

export async function setSeedingPolicy(
  siteKey: string,
  minSeedingHours: number | null,
): Promise<SeedingPolicy> {
  const response = await apiClient.put<SeedingPolicy>(
    `${BASE}/policies/${encodeURIComponent(siteKey)}`,
    { min_seeding_hours: minSeedingHours },
  );
  return response.data;
}
