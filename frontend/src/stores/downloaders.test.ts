import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { ApiProblem } from '../api/client';
import { listDownloaders, setDownloaderEnabled, type Downloader } from '../api/downloaders';
import { useDownloaderStore } from './downloaders';

vi.mock('../api/downloaders', async () => {
  const actual = await vi.importActual<typeof import('../api/downloaders')>('../api/downloaders');
  return {
    ...actual,
    createDownloader: vi.fn(),
    deleteDownloader: vi.fn(),
    diagnoseDownloaderPaths: vi.fn(),
    getDownloader: vi.fn(),
    listDownloaders: vi.fn(),
    setDownloaderEnabled: vi.fn(),
    testDownloader: vi.fn(),
    updateDownloader: vi.fn(),
  };
});

const downloader: Downloader = {
  id: 'store-downloader-001',
  name: 'Store qB',
  type: 'QBITTORRENT',
  base_url: 'http://qb.invalid:8080',
  credential_configured: true,
  monitor_rules: {},
  path_mappings: [],
  capabilities: {},
  connection_status: 'OK',
  path_mapping_status: 'OK',
  enabled: false,
  version: 4,
  last_test_at: null,
  last_path_diagnostic_at: null,
  created_at: '2026-09-09T00:00:00Z',
  updated_at: '2026-09-09T00:00:00Z',
};

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
});

describe('下载器 store', () => {
  it('启用时使用当前版本，并仅用服务端返回值更新状态', async () => {
    vi.mocked(listDownloaders).mockResolvedValue([downloader]);
    vi.mocked(setDownloaderEnabled).mockResolvedValue({
      ...downloader,
      enabled: true,
      version: 5,
    });
    const store = useDownloaderStore();
    await store.refresh();

    await store.setEnabled(store.items[0]!, true);

    expect(setDownloaderEnabled).toHaveBeenCalledWith(downloader.id, 4, true);
    expect(store.items[0]?.enabled).toBe(true);
    expect(store.items[0]?.version).toBe(5);
  });

  it('会话失效时清空先前加载的下载器和诊断结果', async () => {
    vi.mocked(listDownloaders).mockResolvedValueOnce([downloader]);
    const store = useDownloaderStore();
    await store.refresh();
    store.diagnostics[downloader.id] = {
      status: 'ok',
      all_mappings_verified: true,
      error_code: null,
      results: [],
    };

    vi.mocked(listDownloaders).mockRejectedValueOnce(
      new ApiProblem('会话失效', { status: 401, code: 'AUTH_SESSION_INVALID' }),
    );
    await expect(store.refresh()).rejects.toMatchObject({ status: 401 });

    expect(store.items).toEqual([]);
    expect(store.diagnostics).toEqual({});
  });
});
