import { describe, expect, it } from 'vitest';

import type { Preflight, TaskCandidate } from './api/tasks';
import { choosePrimaryCandidate, classifyPreflightReview } from './preflightReviews';

function candidate(
  id: string,
  options: Partial<
    Pick<
      TaskCandidate,
      'score' | 'rejected' | 'selected_for_verification' | 'verification_level' | 'error_code'
    >
  > = {},
): TaskCandidate {
  return {
    id,
    snapshot_id: 'pf-1',
    normalized_unit_key: 'unit',
    site_id: 'site',
    torrent_id: id,
    display_name: id,
    score: options.score ?? 80,
    rejected: options.rejected ?? false,
    selected_for_verification: options.selected_for_verification ?? true,
    verification_level: options.verification_level ?? 'FULL_VERIFIED',
    metainfo_digest: null,
    error_code: options.error_code ?? null,
    evidence: {},
    created_at: '2026-09-10T00:00:00Z',
  };
}

const current = { current: true } as Pick<Preflight, 'current'>;

describe('真实 preflight 审核分类', () => {
  it('stale 优先于候选验证等级，且全部硬拒绝时阻断', () => {
    expect(classifyPreflightReview({ current: false }, [candidate('ok')])).toBe('STALE');
    expect(
      classifyPreflightReview(current, [
        candidate('a', { rejected: true, selected_for_verification: false }),
        candidate('b', { rejected: true, selected_for_verification: false }),
      ]),
    ).toBe('BLOCKED');
  });

  it('只由进入深度验证的可用候选决定 FULL/CLIENT/BLOCKED', () => {
    expect(
      classifyPreflightReview(current, [
        candidate('rejected', { rejected: true, selected_for_verification: false, score: 99 }),
        candidate('full'),
      ]),
    ).toBe('FULL_VERIFIED');
    expect(
      classifyPreflightReview(current, [
        candidate('client', { verification_level: 'CLIENT_CHECK_REQUIRED' }),
      ]),
    ).toBe('CLIENT_CHECK_REQUIRED');
    expect(
      classifyPreflightReview(current, [
        candidate('bad', { error_code: 'FILE_MAPPING_AMBIGUOUS' }),
      ]),
    ).toBe('BLOCKED');
  });

  it('主候选选择忽略硬拒绝并按分数、稳定 id 排序', () => {
    expect(
      choosePrimaryCandidate([
        candidate('z', { score: 90 }),
        candidate('a', { score: 90 }),
        candidate('rejected', { score: 100, rejected: true }),
      ])?.id,
    ).toBe('a');
  });
});
