import { apiClient } from './client';

export type MovieDedupMode = 'AUTO' | 'HARDLINK' | 'SYMLINK' | 'SCAN_ONLY';
export type MovieDedupCrossFilesystemPolicy = 'STOP' | 'SYMLINK';

export interface MovieDedupJob {
  id: string;
  name: string;
  source_root: string;
  target_root: string;
  mode: MovieDedupMode;
  cross_filesystem_policy: MovieDedupCrossFilesystemPolicy;
  include_subdirectories: boolean;
  min_size_bytes: number;
  video_extensions: string[];
  status: string;
  phase: string;
  source_scan_cursor: string | null;
  target_scan_cursor: string | null;
  source_file_count: number;
  target_file_count: number;
  candidate_count: number;
  verified_count: number;
  deduplicated_count: number;
  failed_count: number;
  logical_duplicate_bytes: number;
  estimated_reclaimable_bytes: number;
  version: number;
  error_code: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

export interface MovieDedupPair {
  id: string;
  source_relative_path: string;
  target_relative_path: string;
  source_media_metadata: Record<string, string>;
  target_media_metadata: Record<string, string>;
  source_size_bytes: number;
  target_size_bytes: number;
  source_device: number;
  target_device: number;
  source_inode: number;
  target_inode: number;
  source_link_count: number;
  target_link_count: number;
  source_mtime_ns: string;
  target_mtime_ns: string;
  source_sha256: string | null;
  target_sha256: string | null;
  metadata_match: boolean;
  size_match: boolean;
  quick_hash_match: boolean;
  full_hash_match: boolean;
  status: string;
  resolved_action: 'HARDLINK' | 'SYMLINK' | 'SCAN_ONLY' | 'BLOCKED';
  estimated_reclaimable_bytes: number;
  error_code: string | null;
  error_message: string | null;
}

export interface MovieDedupPrecheck {
  source_root: string;
  target_root: string;
  source_device: number;
  target_device: number;
  same_filesystem: boolean;
  resolved_action: 'HARDLINK' | 'SYMLINK' | 'SCAN_ONLY' | 'BLOCKED';
  blocked_reasons: string[];
}

export interface MovieDedupJobCreateInput {
  name: string;
  source_root: string;
  target_root: string;
  mode: MovieDedupMode;
  cross_filesystem_policy: MovieDedupCrossFilesystemPolicy;
  include_subdirectories: boolean;
  min_size_bytes: number;
  video_extensions: string[];
}

export async function listMovieDedupJobs(): Promise<MovieDedupJob[]> {
  const response = await apiClient.get<{ items: MovieDedupJob[] }>('/movie-dedup/jobs');
  return response.data.items;
}

export async function createMovieDedupJob(
  payload: MovieDedupJobCreateInput,
): Promise<MovieDedupJob> {
  const response = await apiClient.post<MovieDedupJob>('/movie-dedup/jobs', payload);
  return response.data;
}

export async function precheckMovieDedup(
  payload: Pick<
    MovieDedupJobCreateInput,
    'source_root' | 'target_root' | 'mode' | 'cross_filesystem_policy'
  >,
): Promise<MovieDedupPrecheck> {
  const response = await apiClient.post<MovieDedupPrecheck>('/movie-dedup/precheck', payload);
  return response.data;
}

export async function startMovieDedupJob(jobId: string): Promise<MovieDedupJob> {
  const response = await apiClient.post<MovieDedupJob>(`/movie-dedup/jobs/${jobId}/start`);
  return response.data;
}

export async function getMovieDedupJob(jobId: string): Promise<MovieDedupJob> {
  const response = await apiClient.get<MovieDedupJob>(`/movie-dedup/jobs/${jobId}`);
  return response.data;
}

export async function deleteMovieDedupJob(jobId: string): Promise<void> {
  await apiClient.delete(`/movie-dedup/jobs/${jobId}`);
}

export async function listMovieDedupPairs(jobId: string): Promise<MovieDedupPair[]> {
  const response = await apiClient.get<{ items: MovieDedupPair[] }>(
    `/movie-dedup/jobs/${jobId}/pairs`,
    { params: { limit: 500 } },
  );
  return response.data.items;
}

export async function executeMovieDedupPairs(
  jobId: string,
  pairIds: string[],
  idempotencyKey: string,
): Promise<MovieDedupJob> {
  const response = await apiClient.post<MovieDedupJob>(
    `/movie-dedup/jobs/${jobId}/execute`,
    { pair_ids: pairIds },
    { headers: { 'Idempotency-Key': idempotencyKey }, timeout: 0 },
  );
  return response.data;
}
