import { apiClient, toApiProblem } from './client';
import type { components } from './generated/schema';

export type YieldSummary = components['schemas']['YieldSummaryResponse'];
export type YieldTorrent = components['schemas']['YieldTorrentResponse'];
export type YieldTorrentList = components['schemas']['YieldTorrentListResponse'];
export type YieldRefreshResult = components['schemas']['YieldRefreshResponse'];
export type CleanupPreview = components['schemas']['CleanupPreviewResponse'];

const BASE = '/monitor/yield';

export async function getYieldSummary(days: number): Promise<YieldSummary> {
  const response = await apiClient.get<YieldSummary>(`${BASE}/summary`, {
    params: { days },
  });
  return response.data;
}

export async function listYieldTorrents(
  days: number,
  options: {
    sort?: 'upload' | 'bonus' | 'hourly';
    order?: 'asc' | 'desc';
    zeroOnly?: boolean;
    query?: string;
    site?: string;
    page?: number;
    pageSize?: number;
  } = {},
): Promise<YieldTorrentList> {
  const response = await apiClient.get<YieldTorrentList>(`${BASE}/torrents`, {
    params: {
      days,
      sort: options.sort ?? 'upload',
      order: options.order ?? 'desc',
      zero_only: options.zeroOnly ?? false,
      q: options.query || undefined,
      site: options.site || undefined,
      page: options.page ?? 1,
      page_size: options.pageSize ?? 20,
    },
  });
  return response.data;
}

export async function refreshYield(): Promise<YieldRefreshResult> {
  const response = await apiClient.post<YieldRefreshResult>(`${BASE}/refresh`);
  return response.data;
}

export async function previewCleanup(keys: string[]): Promise<CleanupPreview> {
  const response = await apiClient.post<CleanupPreview>(`${BASE}/cleanup-preview`, { keys });
  return response.data;
}

export async function exportCleanupCsv(keys: string[]): Promise<{ blob: Blob; filename: string }> {
  try {
    const response = await apiClient.post<Blob>(
      `${BASE}/cleanup-export`,
      { keys },
      { responseType: 'blob' },
    );
    return { blob: response.data, filename: 'torrent-yield-cleanup.csv' };
  } catch (error) {
    throw toApiProblem(error);
  }
}
