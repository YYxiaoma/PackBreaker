import { apiClient, toApiProblem } from './client';
import type { components } from './generated/schema';

export type SiteAssetSummary = components['schemas']['SiteAssetSummaryResponse'];
export type SiteAssetPoint = components['schemas']['SiteAssetPointResponse'];
export type SiteAssetSite = components['schemas']['SiteAssetSiteResponse'];
export type SiteAssetSiteList = components['schemas']['SiteAssetSiteListResponse'];

const BASE = '/monitor/site-assets';

export async function getSiteAssetSummary(days: number): Promise<SiteAssetSummary> {
  const response = await apiClient.get<SiteAssetSummary>(`${BASE}/summary`, {
    params: { days },
  });
  return response.data;
}

export async function getSiteAssetTrend(days: number): Promise<SiteAssetPoint[]> {
  const response = await apiClient.get<SiteAssetPoint[]>(`${BASE}/trend`, {
    params: { days },
  });
  return response.data;
}

export async function listSiteAssetSites(
  days: number,
  options: { query?: string; page?: number; pageSize?: number } = {},
): Promise<SiteAssetSiteList> {
  const response = await apiClient.get<SiteAssetSiteList>(`${BASE}/sites`, {
    params: {
      days,
      q: options.query || undefined,
      page: options.page ?? 1,
      page_size: options.pageSize ?? 20,
    },
  });
  return response.data;
}

export async function exportSiteAssetCsv(days: number): Promise<{ blob: Blob; filename: string }> {
  try {
    const response = await apiClient.get<Blob>(`${BASE}/export`, {
      params: { days },
      responseType: 'blob',
    });
    const disposition = String(response.headers?.['content-disposition'] ?? '');
    const match = /filename="([^"]+)"/.exec(disposition);
    return {
      blob: response.data,
      filename: match?.[1] || `site-assets-${days}d.csv`,
    };
  } catch (error) {
    throw toApiProblem(error);
  }
}
