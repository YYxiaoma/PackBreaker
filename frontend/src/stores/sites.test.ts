import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { ApiProblem } from '../api/client';
import {
  getSiteHealth,
  getSiteUserProfile,
  listSiteProfiles,
  listSites,
  deleteSite,
  resetSiteCircuit,
  setSiteEnabled,
  updateSite,
  type Site,
  type SiteUserProfile,
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
    getSiteUserProfile: vi.fn(),
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

const userProfile: SiteUserProfile = {
  site_id: 'mteam',
  uid: '42',
  username: 'SyntheticUser',
  user_level: null,
  real_uploaded_bytes: null,
  real_downloaded_bytes: null,
  uploaded_bytes: 200,
  downloaded_bytes: 100,
  ratio: 2,
  torrents_posted: null,
  seeding_count: 8,
  seeding_size_bytes: null,
  bonus: null,
  seeding_points: null,
  bonus_per_hour: null,
  fetched_at: '2026-09-17T12:00:00Z',
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
    await store.update(store.items[0]!, { name: '新版配置', clear_credential: false });
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
    await store.update(store.items[0]!, { name: '新配置', clear_credential: false });
    resolveOld({ ...health, config_version: 4, circuit_state: 'CLOSED' });
    await oldReset;
    expect(store.health[site.id]).toMatchObject({ config_version: 5, circuit_state: 'OPEN' });
  });

  it('列表刷新不抓用户详情，只有显式打开详情时才请求 profile', async () => {
    vi.mocked(listSites).mockResolvedValue([site]);
    vi.mocked(getSiteUserProfile).mockResolvedValue(userProfile);
    const store = useSiteStore();

    await store.refresh();
    expect(getSiteUserProfile).not.toHaveBeenCalled();

    await store.loadUserProfile(store.items[0]!);
    expect(getSiteUserProfile).toHaveBeenCalledWith(site.id);
    expect(store.userProfiles[site.id]).toEqual(userProfile);
  });

  it('刷新资料时不继续展示旧统计，失败后只显示错误', async () => {
    vi.mocked(listSites).mockResolvedValue([site]);
    vi.mocked(getSiteUserProfile).mockResolvedValueOnce(userProfile);
    const store = useSiteStore();
    await store.refresh();
    await store.loadUserProfile(store.items[0]!);
    expect(store.userProfiles[site.id]).toEqual(userProfile);

    vi.mocked(getSiteUserProfile).mockRejectedValueOnce(
      new ApiProblem('资料暂不可用', { status: 503, code: 'SITE_UNAVAILABLE' }),
    );
    const refreshing = store.loadUserProfile(store.items[0]!);
    expect(store.userProfiles[site.id]).toBeUndefined();
    await expect(refreshing).rejects.toMatchObject({ status: 503 });
    expect(store.userProfiles[site.id]).toBeUndefined();
    expect(store.userProfileErrors[site.id]?.code).toBe('SITE_UNAVAILABLE');
  });

  it('配置更新立即失效旧资料，旧版本的未完成请求不能覆盖新版本', async () => {
    vi.mocked(listSites).mockResolvedValue([site]);
    const store = useSiteStore();
    await store.refresh();

    let resolveOld!: (profile: SiteUserProfile) => void;
    vi.mocked(getSiteUserProfile).mockImplementationOnce(
      () => new Promise<SiteUserProfile>((resolve) => (resolveOld = resolve)),
    );
    const oldRequest = store.loadUserProfile(store.items[0]!);
    vi.mocked(updateSite).mockResolvedValueOnce({ ...site, name: '更新后的站点', version: 5 });
    await store.update(store.items[0]!, { name: '更新后的站点', clear_credential: false });
    expect(store.userProfiles[site.id]).toBeUndefined();

    const currentProfile = { ...userProfile, username: 'CurrentSyntheticUser' };
    vi.mocked(getSiteUserProfile).mockResolvedValueOnce(currentProfile);
    await store.loadUserProfile(store.items[0]!);
    expect(store.userProfiles[site.id]).toEqual(currentProfile);
    resolveOld(userProfile);
    await oldRequest;
    expect(store.userProfiles[site.id]).toEqual(currentProfile);
    expect(store.busy[`profile:${site.id}`]).toBeUndefined();
  });

  it('删除站点后旧资料请求返回也不能恢复已删除用户信息', async () => {
    vi.mocked(listSites).mockResolvedValue([site]);
    const store = useSiteStore();
    await store.refresh();

    let resolveOld!: (profile: SiteUserProfile) => void;
    vi.mocked(getSiteUserProfile).mockImplementationOnce(
      () => new Promise<SiteUserProfile>((resolve) => (resolveOld = resolve)),
    );
    const pending = store.loadUserProfile(store.items[0]!);
    vi.mocked(deleteSite).mockResolvedValueOnce(undefined);
    await store.remove(store.items[0]!);
    resolveOld(userProfile);
    await pending;
    expect(store.items).toEqual([]);
    expect(store.userProfiles[site.id]).toBeUndefined();
    expect(store.userProfileErrors[site.id]).toBeUndefined();
  });

  it('旧配置的迟到 401 不得清空新配置已经成功读取的资料', async () => {
    vi.mocked(listSites).mockResolvedValue([site]);
    const store = useSiteStore();
    await store.refresh();

    let rejectOld!: (reason: unknown) => void;
    vi.mocked(getSiteUserProfile).mockImplementationOnce(
      () => new Promise<SiteUserProfile>((_resolve, reject) => (rejectOld = reject)),
    );
    const staleResult = store.loadUserProfile(store.items[0]!).catch((caught: unknown) => caught);
    vi.mocked(updateSite).mockResolvedValueOnce({ ...site, version: 5 });
    await store.update(store.items[0]!, { name: '已更新配置', clear_credential: false });
    const currentProfile = { ...userProfile, username: 'NewSyntheticUser' };
    vi.mocked(getSiteUserProfile).mockResolvedValueOnce(currentProfile);
    await store.loadUserProfile(store.items[0]!);

    rejectOld(new ApiProblem('旧凭据已失效', { status: 401, code: 'AUTH_SESSION_INVALID' }));
    expect(await staleResult).toMatchObject({ status: 401 });
    expect(store.items).toHaveLength(1);
    expect(store.userProfiles[site.id]).toEqual(currentProfile);
    expect(store.userProfileErrors[site.id]).toBeUndefined();
  });

  it('列表同步发现配置版本变化时清除之前缓存的站点资料', async () => {
    vi.mocked(listSites).mockResolvedValueOnce([site]);
    vi.mocked(getSiteUserProfile).mockResolvedValueOnce(userProfile);
    const store = useSiteStore();
    await store.refresh();
    await store.loadUserProfile(store.items[0]!);
    expect(store.userProfiles[site.id]).toEqual(userProfile);

    vi.mocked(listSites).mockResolvedValueOnce([{ ...site, version: 5 }]);
    await store.refresh();
    expect(store.userProfiles[site.id]).toBeUndefined();
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
