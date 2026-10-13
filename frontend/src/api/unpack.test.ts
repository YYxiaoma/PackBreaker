import { beforeEach, describe, expect, it, vi } from 'vitest';

import { apiClient } from './client';
import {
  browseUnpackTree,
  createUnpackSourceScan,
  deleteUnpackExecutionItem,
  getUnpackExecution,
  getUnpackItemDiagnostics,
  getUnpackItemLiveClientState,
  listUnpackExecutionItems,
  listUnpackItemCandidates,
  listUnpackSourceScanItems,
  retryUnpackItemMatch,
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
