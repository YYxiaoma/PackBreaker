import { apiClient, toApiProblem } from './client';
import type { components } from './generated/schema';

export type CoverageSummary = components['schemas']['CoverageSummaryResponse'];
export type CoverageRun = components['schemas']['CoverageRunResponse'];
export type CoverageMovie = components['schemas']['CoverageMovieResponse'];
export type CoverageMovieList = components['schemas']['CoverageMovieListResponse'];
export type CoverageScope = components['schemas']['CoverageScopeResponse'];

const BASE = '/monitor/coverage';

export async function getCoverageSummary(): Promise<CoverageSummary> {
  const response = await apiClient.get<CoverageSummary>(`${BASE}/summary`);
  return response.data;
}

export async function listCoverageMovies(
  bucket: string | null,
  options: { query?: string; resolution?: string; page?: number; pageSize?: number } = {},
): Promise<CoverageMovieList> {
  const response = await apiClient.get<CoverageMovieList>(`${BASE}/movies`, {
    params: {
      bucket: bucket ?? undefined,
      q: options.query || undefined,
      resolution: options.resolution || undefined,
      page: options.page ?? 1,
      page_size: options.pageSize ?? 20,
    },
  });
  return response.data;
}

export async function listCoverageRuns(limit = 20): Promise<CoverageRun[]> {
  const response = await apiClient.get<CoverageRun[]>(`${BASE}/runs`, { params: { limit } });
  return response.data;
}

export async function getCoverageScope(): Promise<CoverageScope | null> {
  const response = await apiClient.get<CoverageScope | null>(`${BASE}/scope`);
  return response.data;
}

export async function saveCoverageScope(
  selectionToken: string,
  scopeLabel: string,
): Promise<CoverageScope> {
  const response = await apiClient.put<CoverageScope>(`${BASE}/scope`, {
    selection_token: selectionToken,
    scope_label: scopeLabel,
  });
  return response.data;
}

export async function startCoverageRun(selectionToken: string): Promise<string> {
  const response = await apiClient.post<{ run_id: string; accepted: boolean }>(`${BASE}/runs`, {
    selection_token: selectionToken,
  });
  return response.data.run_id;
}

export async function exportCoverageCsv(
  bucket: string | null,
): Promise<{ blob: Blob; filename: string }> {
  try {
    const response = await apiClient.get<Blob>(`${BASE}/export`, {
      params: { bucket: bucket ?? undefined },
      responseType: 'blob',
    });
    return {
      blob: response.data,
      filename: `coverage-${bucket ?? 'all'}.csv`,
    };
  } catch (error) {
    throw toApiProblem(error);
  }
}
