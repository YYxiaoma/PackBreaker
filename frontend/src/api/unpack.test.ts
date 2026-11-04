import { beforeEach, describe, expect, it, vi } from 'vitest';

import { apiClient } from './client';
import {
  browseUnpackTree,
  createUnpackSourceScan,
  previewManualUnpackCandidate,
  addManualUnpackCandidate,
  deleteUnpackExecutionItem,
  getUnpackExecution,
  getUnpackItemDiagnostics,
  getUnpackItemLiveClientState,
  listUnpackExecutionItems,
  listUnpackItemCandidates,
  listUnpackSourceScanItems,
  retryUnpackItemMatch,
  retryUnpackExecutionItems,
  reviewUnpackItem,
  runUnpackDefinition,
  updateUnpackSourceScanSelection,
} from './unpack';

describe('unpack api', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('passes opaque directory token without decoding it in the frontend', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({
      data: { display_path: '/data/movies', selection_token: 'v1.token', entries: [] },
      headers: {},
    });

    await browseUnpackTree('v1.token');

    expect(get).toHaveBeenCalledWith('/files/tree', {
      params: { selection_token: 'v1.token' },
    });
  });

  it('creates source scans and preserves file filters', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: {
        id: 'scan-1',
        directory_path: '/data/movies',
        discovered_count: 2,
        selected_count: 0,
        expires_at: '2026-10-07T06:00:00Z',
        version: 1,
      },
      headers: {},
    });

    await createUnpackSourceScan({
      selection_token: 'v1.token',
      file_filter: { extensions: ['.mkv'] },
    });

    expect(post).toHaveBeenCalledWith('/unpack/source-scans', {
      selection_token: 'v1.token',
      file_filter: { extensions: ['.mkv'] },
    });
  });

  it('passes media filters and explicit selected=false', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({
      data: { items: [], next_cursor: null, has_more: false },
      headers: {},
    });

    await listUnpackSourceScanItems('scan-1', {
      resolution: '2160p',
      selected: false,
      limit: 50,
    });

    expect(get).toHaveBeenCalledWith('/unpack/source-scans/scan-1/items', {
      params: { limit: 50, resolution: '2160p', selected: false },
    });
  });

  it('updates selection and runs saved definitions through separate calls', async () => {
    const put = vi.spyOn(apiClient, 'put').mockResolvedValue({
      data: { discovered_count: 2, selected_count: 1 },
      headers: {},
    });
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: { definition: { id: 'definition-1' }, execution_id: 'execution-1' },
      headers: {},
    });

    await updateUnpackSourceScanSelection('scan-1', {
      source_object_keys: ['media-key'],
      selected: true,
    });
    await runUnpackDefinition('definition-1');

    expect(put).toHaveBeenCalledWith('/unpack/source-scans/scan-1/selection', {
      source_object_keys: ['media-key'],
      selected: true,
    });
    expect(post).toHaveBeenCalledWith('/unpack/definitions/definition-1/actions', {
      action: 'run',
    });
  });

  it('bulk retries eligible child items with version check and result summary', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: { execution_id: 'execution-1', retried_count: 4, skipped_unsafe_count: 2 },
      headers: {},
    });
    const result = await retryUnpackExecutionItems('execution-1', 7, [
      'NO_MATCH',
      'REVIEW_REQUIRED',
    ]);
    expect(result.retried_count).toBe(4);
    expect(result.skipped_unsafe_count).toBe(2);
    expect(post).toHaveBeenCalledWith(
      '/unpack/executions/execution-1/actions',
      { action: 'retry_eligible_items', item_statuses: ['NO_MATCH', 'REVIEW_REQUIRED'] },
      { headers: { 'If-Match': '7' } },
    );
  });

  it('reads execution summary and item pages', async () => {
    const get = vi
      .spyOn(apiClient, 'get')
      .mockResolvedValueOnce({ data: { id: 'execution-1', status: 'MATCHING' }, headers: {} })
      .mockResolvedValueOnce({
        data: { items: [], next_cursor: null, has_more: false },
        headers: {},
      });

    await getUnpackExecution('execution-1');
    await listUnpackExecutionItems('execution-1', { status: 'MATCH_PENDING', limit: 50 });

    expect(get).toHaveBeenNthCalledWith(1, '/unpack/executions/execution-1');
    expect(get).toHaveBeenNthCalledWith(2, '/unpack/executions/execution-1/items', {
      params: { limit: 50, item_status: 'MATCH_PENDING' },
    });
  });

  it('fetches historical operation diagnostics through GET only', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({
      data: {
        item_id: 'item-1',
        status: 'EXECUTION_ERROR',
        execution_stage: 'STARTING',
        requires_manual_reconciliation: true,
        downloader_state_live_checked: false,
        filesystem_state_live_checked: false,
        operations: [],
      },
      headers: {},
    });
    const result = await getUnpackItemDiagnostics('item-1');
    expect(get).toHaveBeenCalledExactlyOnceWith('/unpack/items/item-1/diagnostics');
    expect(result.downloader_state_live_checked).toBe(false);
    expect(result.requires_manual_reconciliation).toBe(true);
  });

  it('performs an explicit live status GET with no downloader write API', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({
      data: { status: 'UNKNOWN', error_code: 'DOWNLOADER_READ_UNAVAILABLE' },
      headers: {},
    });
    const result = await getUnpackItemLiveClientState('item-1');
    expect(get).toHaveBeenCalledExactlyOnceWith('/unpack/items/item-1/diagnostics/live');
    expect(result.status).toBe('UNKNOWN');
  });

  it('reads candidates without submitting the default review choice', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({
      data: {
        item_id: 'item-1',
        generation: 2,
        item_version: 3,
        default_candidate_id: 'candidate-1',
        candidates: [],
      },
      headers: {},
    });

    const result = await listUnpackItemCandidates('item-1');

    expect(get).toHaveBeenCalledWith('/unpack/items/item-1/candidates');
    expect(result.default_candidate_id).toBe('candidate-1');
    expect(result.candidates).toEqual([]);
  });

  it('submits review with item version and stable idempotency key', async () => {
    const put = vi.spyOn(apiClient, 'put').mockResolvedValue({
      data: {
        decision_id: 'review-1',
        item_id: 'item-1',
        decision: 'APPROVE',
        candidate_id: 'candidate-1',
        generation: 2,
        item_version: 4,
        item_status: 'MATCHED_MANUAL',
        decided_at: '2026-10-07T10:00:00Z',
        replayed: false,
      },
      headers: {},
    });

    await reviewUnpackItem(
      'item-1',
      { decision: 'APPROVE', candidate_id: 'candidate-1', generation: 2 },
      { itemVersion: 3, idempotencyKey: 'review-nonce-1' },
    );

    expect(put).toHaveBeenCalledWith(
      '/unpack/items/item-1/review',
      { decision: 'APPROVE', candidate_id: 'candidate-1', generation: 2 },
      {
        headers: {
          'If-Match': '3',
          'Idempotency-Key': 'review-nonce-1',
        },
      },
    );
  });

  it('retries matching with If-Match and no hidden execution request', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: {
        item_id: 'item-1',
        generation: 3,
        retry_count: 1,
        item_version: 4,
        item_status: 'MATCH_PENDING',
      },
      headers: {},
    });

    await retryUnpackItemMatch('item-1', 3);

    expect(post).toHaveBeenCalledWith(
      '/unpack/items/item-1/actions',
      { action: 'retry_match' },
      { headers: { 'If-Match': '3' } },
    );
  });

  it('previews a manually supplied site URL as JSON without persisting a candidate', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: {
        site_id: 'site-mteam',
        site_name: 'M-TEAM',
        source_kind: 'URL',
        source_title: 'Movie.2024.mkv',
        source_size_bytes: 100,
        candidate_key: '12345',
        candidate_title: 'Movie.2024.1080p.BluRay-MNHD',
        torrent_name: 'Movie.2024.1080p.mkv',
        torrent_kind: 'V1',
        v1_info_hash: 'a'.repeat(40),
        v2_info_hash: null,
        metainfo_digest: 'b'.repeat(64),
        payload_sha256: 'c'.repeat(64),
        total_size_bytes: 100,
        file_count: 1,
        files: [],
        score_bps: 9900,
        evidence: { dimensions: [] },
        hard_conflicts: [],
        seeders: 7,
        imdb_id: null,
        douban_id: null,
      },
      headers: {},
    });

    await previewManualUnpackCandidate('item-1', {
      siteId: 'site-mteam',
      generation: 2,
      itemVersion: 3,
      sourceUrl: 'https://mteam.example/details.php?id=12345',
    });

    expect(post).toHaveBeenCalledWith('/unpack/items/item-1/manual-candidate-preview', {
      site_id: 'site-mteam',
      expected_generation: 2,
      expected_item_version: 3,
      source_url: 'https://mteam.example/details.php?id=12345',
    });
  });

  it('encodes a manually uploaded torrent and binds confirmation to both digests', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: {
        candidate_id: 'candidate-manual',
        candidate_key: 'manual-content-key',
        generation: 2,
        item_version: 4,
      },
      headers: {},
    });
    const file = new File([new Uint8Array([0, 1, 2, 255])], 'seed.torrent');

    await addManualUnpackCandidate(
      'item-1',
      { siteId: 'site-mteam', generation: 2, itemVersion: 3, file },
      'a'.repeat(64),
      'b'.repeat(64),
    );

    expect(post).toHaveBeenCalledWith('/unpack/items/item-1/manual-candidates', {
      site_id: 'site-mteam',
      expected_generation: 2,
      expected_item_version: 3,
      torrent_file_base64: 'AAEC/w==',
      torrent_file_name: 'seed.torrent',
      expected_metainfo_digest: 'a'.repeat(64),
      expected_payload_sha256: 'b'.repeat(64),
    });
  });

  it('deletes only an execution item record using an explicit version', async () => {
    const request = vi
      .spyOn(apiClient, 'delete')
      .mockResolvedValue({ data: undefined, headers: {} });
    await deleteUnpackExecutionItem('item-1', 3);
    expect(request).toHaveBeenCalledWith('/unpack/items/item-1', {
      headers: { 'If-Match': '3' },
    });
  });
});
