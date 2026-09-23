import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { ApiProblem } from '../api/client';
import {
  getSiteHealth,
  listSiteProfiles,
  listSites,
  deleteSite,
  resetSiteCircuit,
  setSiteEnabled,
  updateSite,
  type Site,
} from '../api/sites';
import { useSiteStore } from './sites';

vi.mock('../api/sites', async () => {
  const actual = await vi.importActual<typeof import('../api/sites')>('../api/sites');
  return {
    ...actual,
    createSite: vi.fn(),
    deleteSite: vi.fn(),
    getSite: vi.fn(),
    getSiteHealth: vi.fn(),
    listSiteProfiles: vi.fn(),
    listSites: vi.fn(),
    resetSiteCircuit: vi.fn(),
    setSiteEnabled: vi.fn(),
    testSite: vi.fn(),
    updateSite: vi.fn(),
  };
});

const site: Site = {
  id: 'site-store-001',
  name: 'M-Team 主站',
  type: 'MTEAM',
  base_url: 'https://api.m-team.cc',
  credential_kind: 'API_KEY',
  credential_configured: true,
  download_credential_configured: false,
  request_timeout_seconds: 15,
  search_interval_seconds: 0,
  user_agent: null,
  browser_emulation_enabled: false,
  proxy_enabled: false,
  proxy_host: null,
  proxy_port: null,
  proxy_username: null,
  proxy_credential_configured: false,
  capabilities: {},
  connection_status: 'OK',
  enabled: false,
  version: 4,
  last_test_at: null,
  created_at: '2026-09-13T00:00:00Z',
  updated_at: '2026-09-13T00:00:00Z',
};

const health = {
  config_version: 4,
  circuit_state: 'CLOSED' as const,
  failure_count: 0,
  retry_after_seconds: null,
  half_open_probe_in_flight: false,
  rate_limit_wait_seconds: 0,
  cache_entries: 0,
  cache_hits: 0,
  cache_misses: 0,
  cache_evictions: 0,
  requests_started: 0,
  requests_succeeded: 0,
  requests_failed: 0,
  retries_scheduled: 0,
  last_error_code: null,
};

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
  vi.mocked(listSiteProfiles).mockResolvedValue([]);
  vi.mocked(getSiteHealth).mockResolvedValue(health);
});

describe('站点 store', () => {
  it('启用时使用当前 version，并只采用服务端返回状态', async () => {
    vi.mocked(listSites).mockResolvedValue([site]);
    vi.mocked(setSiteEnabled).mockResolvedValue({ ...site, enabled: true, version: 5 });
    vi.mocked(getSiteHealth).mockResolvedValue({ ...health, config_version: 5 });
    const store = useSiteStore();
    await store.refresh();

    await store.setEnabled(store.items[0]!, true);

    expect(setSiteEnabled).toHaveBeenCalledWith(site.id, 4, true);
    expect(store.items[0]?.enabled).toBe(true);
    expect(store.items[0]?.version).toBe(5);
    expect(store.health[site.id]?.config_version).toBe(5);
  });

  it('单站 health 暂不可用时保留配置列表，不伪造健康状态', async () => {
    vi.mocked(listSites).mockResolvedValue([site]);
    vi.mocked(getSiteHealth).mockRejectedValueOnce(
      new ApiProblem('health unavailable', { status: 502, code: 'SITE_HEALTH_UNAVAILABLE' }),
    );
    const store = useSiteStore();

    await store.refresh();

    expect(store.items).toHaveLength(1);
    expect(store.health[site.id]).toBeUndefined();
  });

  it('旧版健康状态请求迟到时不得覆盖新版健康状态', async () => {
    vi.mocked(listSites).mockResolvedValue([site]);
    const store = useSiteStore();
    await store.refresh();

    let resolveOld!: (value: typeof health) => void;
    vi.mocked(getSiteHealth).mockImplementationOnce(
      () => new Promise<typeof health>((resolve) => (resolveOld = resolve)),
    );
    const oldHealth = store.refreshHealth(store.items[0]!);
    vi.mocked(updateSite).mockResolvedValueOnce({ ...site, version: 5 });
    vi.mocked(getSiteHealth).mockResolvedValueOnce({
      ...health,
      config_version: 5,
      circuit_state: 'OPEN',
    });
    await store.update(store.items[0]!, {
      name: '新版配置',
      clear_credential: false,
      clear_download_cookie: false,
    });
    resolveOld({ ...health, config_version: 4, circuit_state: 'CLOSED' });
    await oldHealth;
    expect(store.health[site.id]).toMatchObject({ config_version: 5, circuit_state: 'OPEN' });
  });

  it('已删除站点的迟到健康结果不能恢复健康缓存', async () => {
    vi.mocked(listSites).mockResolvedValue([site]);
    const store = useSiteStore();
    await store.refresh();

    let resolveOld!: (value: typeof health) => void;
    vi.mocked(getSiteHealth).mockImplementationOnce(
      () => new Promise<typeof health>((resolve) => (resolveOld = resolve)),
    );
    const oldHealth = store.refreshHealth(store.items[0]!);
    vi.mocked(deleteSite).mockResolvedValueOnce(undefined);
    await store.remove(store.items[0]!);
    resolveOld(health);
    await oldHealth;
    expect(store.health[site.id]).toBeUndefined();
  });

  it('健康接口返回的配置版本与当前站点版本不一致时不能标记为当前状态', async () => {
    vi.mocked(listSites).mockResolvedValue([{ ...site, version: 5 }]);
    vi.mocked(getSiteHealth).mockResolvedValue({ ...health, config_version: 4 });
    const store = useSiteStore();
    await store.refresh();
    expect(store.health[site.id]).toBeUndefined();
  });

  it('旧版熔断重置的迟到响应不得覆盖新版健康状态', async () => {
    vi.mocked(listSites).mockResolvedValue([site]);
    const store = useSiteStore();
    await store.refresh();

    let resolveOld!: (value: typeof health) => void;
    vi.mocked(resetSiteCircuit).mockImplementationOnce(
      () => new Promise<typeof health>((resolve) => (resolveOld = resolve)),
    );
    const oldReset = store.resetCircuit(store.items[0]!);
    vi.mocked(updateSite).mockResolvedValueOnce({ ...site, version: 5 });
    vi.mocked(getSiteHealth).mockResolvedValueOnce({
      ...health,
      config_version: 5,
      circuit_state: 'OPEN',
    });
    await store.update(store.items[0]!, {
      name: '新配置',
      clear_credential: false,
      clear_download_cookie: false,
    });
    resolveOld({ ...health, config_version: 4, circuit_state: 'CLOSED' });
    await oldReset;
    expect(store.health[site.id]).toMatchObject({ config_version: 5, circuit_state: 'OPEN' });
  });

  it('站点列表与健康状态刷新不产生用户详情请求或缓存', async () => {
    vi.mocked(listSites).mockResolvedValue([site]);
    const store = useSiteStore();
    await store.refresh();
    expect(store.items).toHaveLength(1);
    expect(store.health[site.id]?.config_version).toBe(site.version);
    expect('loadUserProfile' in store).toBe(false);
    expect('userProfiles' in store).toBe(false);
  });

  it('会话失效时清空先前加载的站点和健康状态', async () => {
    vi.mocked(listSites).mockResolvedValueOnce([site]);
    const store = useSiteStore();
    await store.refresh();

    vi.mocked(listSites).mockRejectedValueOnce(
      new ApiProblem('会话失效', { status: 401, code: 'AUTH_SESSION_INVALID' }),
    );
    await expect(store.refresh()).rejects.toMatchObject({ status: 401 });

    expect(store.items).toEqual([]);
    expect(store.health).toEqual({});
  });
});
