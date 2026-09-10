import type { Preflight, TaskCandidate, TaskRecord } from './api/tasks';

export type PreflightReviewLevel =
  | 'STALE'
  | 'BLOCKED'
  | 'CLIENT_CHECK_REQUIRED'
  | 'FULL_VERIFIED'
  | 'NOT_VERIFIED';

export interface PreflightReviewItem {
  task: TaskRecord;
  preflight: Preflight;
  candidates: TaskCandidate[];
  level: PreflightReviewLevel;
  primaryCandidate: TaskCandidate | null;
  hardRejectedCount: number;
}

export function buildPreflightReview(
  task: TaskRecord,
  preflight: Preflight,
  candidates: TaskCandidate[],
): PreflightReviewItem {
  return {
    task,
    preflight,
    candidates,
    level: classifyPreflightReview(preflight, candidates),
    primaryCandidate: choosePrimaryCandidate(candidates),
    hardRejectedCount: candidates.filter((item) => item.rejected).length,
  };
}

export function classifyPreflightReview(
  preflight: Pick<Preflight, 'current'>,
  candidates: TaskCandidate[],
): PreflightReviewLevel {
  if (!preflight.current) return 'STALE';

  const selected = candidates.filter((item) => item.selected_for_verification && !item.rejected);
  if (
    selected.some((item) => item.verification_level === 'BLOCKED' || item.error_code !== null) ||
    (candidates.length > 0 && candidates.every((item) => item.rejected))
  ) {
    return 'BLOCKED';
  }
  if (selected.some((item) => item.verification_level === 'CLIENT_CHECK_REQUIRED')) {
    return 'CLIENT_CHECK_REQUIRED';
  }
  if (
    selected.length > 0 &&
    selected.every((item) => item.verification_level === 'FULL_VERIFIED')
  ) {
    return 'FULL_VERIFIED';
  }
  return 'NOT_VERIFIED';
}

export function choosePrimaryCandidate(candidates: TaskCandidate[]): TaskCandidate | null {
  const eligible = candidates.filter((item) => !item.rejected);
  if (!eligible.length) return null;
  return [...eligible].sort(
    (left, right) => right.score - left.score || left.id.localeCompare(right.id),
  )[0];
}
