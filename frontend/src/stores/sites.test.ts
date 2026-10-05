import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { ApiProblem } from '../api/client';
import { listSiteProfiles, listSites, setSiteEnabled, type Site } from '../api/sites';
import { useSiteStore } from './sites';

vi.mock('../api/sites', async () => {
  const actual = await vi.importActual<typeof import('../api/sites')>('../api/sites');
  return {
    ...actual,
    createSite: vi.fn(),
    deleteSite: vi.fn(),
    getSite: vi.fn(),
    listSiteProfiles: vi.fn(),
    listSites: vi.fn(),
    probeSite: vi.fn(),
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

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
  vi.mocked(listSiteProfiles).mockResolvedValue([]);
});

describe('站点 store', () => {
  it('启用时使用当前 version，并只采用服务端返回状态', async () => {
    vi.mocked(listSites).mockResolvedValue([site]);
    vi.mocked(setSiteEnabled).mockResolvedValue({ ...site, enabled: true, version: 5 });
    const store = useSiteStore();
    await store.refresh();

    await store.setEnabled(store.items[0]!, true);

    expect(setSiteEnabled).toHaveBeenCalledWith(site.id, 4, true);
    expect(store.items[0]?.enabled).toBe(true);
    expect(store.items[0]?.version).toBe(5);
  });

  it('刷新只读取站点与 profile，不维护前端熔断健康缓存', async () => {
    vi.mocked(listSites).mockResolvedValue([site]);
    const store = useSiteStore();

    await store.refresh();

    expect(store.items).toHaveLength(1);
    expect('health' in store).toBe(false);
    expect('refreshHealth' in store).toBe(false);
    expect('resetCircuit' in store).toBe(false);
  });

  it('会话失效时清空先前加载的站点与 profile', async () => {
    vi.mocked(listSites).mockResolvedValueOnce([site]);
    const store = useSiteStore();
    await store.refresh();

    vi.mocked(listSites).mockRejectedValueOnce(
      new ApiProblem('会话失效', { status: 401, code: 'AUTH_SESSION_INVALID' }),
    );
    await expect(store.refresh()).rejects.toMatchObject({ status: 401 });

    expect(store.items).toEqual([]);
    expect(store.profiles).toEqual([]);
  });
});
