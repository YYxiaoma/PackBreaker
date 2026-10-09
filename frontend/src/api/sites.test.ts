import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiClient } from './client';
import {
  credentialKindForSite,
  deleteSite,
  setSiteEnabled,
  siteProfileOptionLabel,
  updateSite,
} from './sites';

afterEach(() => vi.restoreAllMocks());

describe('站点 API 并发前置条件', () => {
  it('站点类型只映射到各自允许的凭证类型', () => {
    expect(credentialKindForSite('MTEAM')).toBe('API_KEY');
    expect(credentialKindForSite('HDTIME')).toBe('COOKIE');
    expect(credentialKindForSite('ROUSI_PRO')).toBe('API_KEY');
  });

  it('常规站点不再显示适配完成文案；待验收七站保持明确警示', () => {
    expect(siteProfileOptionLabel({ display_name: 'KeepFrds', support_status: 'SUPPORTED' })).toBe(
      'KeepFrds',
    );
    for (const displayName of [
      'PTerClub',
      'Audiences',
      'SpringSunday',
      'HDDolby',
      'U2',
      '不可躺',
      'CarPT',
    ]) {
      expect(
        siteProfileOptionLabel({
          display_name: displayName,
          support_status: 'PENDING_REAL_VALIDATION',
        }),
      ).toBe(`${displayName} · 待真实验收`);
    }
    expect(
      siteProfileOptionLabel({ display_name: 'Future Site', support_status: 'PENDING_ADAPTER' }),
    ).toBe('Future Site · 待适配');
  });

  it('PATCH 使用当前 version 生成强 If-Match', async () => {
    const patch = vi.spyOn(apiClient, 'patch').mockResolvedValue({ data: { id: 'site-1' } });

    await updateSite('site/with slash', 7, {
      name: '新名称',
      clear_credential: false,
      clear_download_cookie: false,
    });

    expect(patch).toHaveBeenCalledWith(
      '/sites/site%2Fwith%20slash',
      { name: '新名称', clear_credential: false, clear_download_cookie: false },
      { headers: { 'If-Match': '"7"' } },
    );
  });

  it('删除与启停都携带当前版本', async () => {
    const remove = vi.spyOn(apiClient, 'delete').mockResolvedValue({ data: undefined });
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({ data: {} });

    await deleteSite('site-1', 4);
    await setSiteEnabled('site-1', 4, true);

    expect(remove).toHaveBeenCalledWith('/sites/site-1', {
      headers: { 'If-Match': '"4"' },
    });
    expect(post.mock.calls[0]).toEqual([
      '/sites/site-1/actions',
      { action: 'enable' },
      { headers: { 'If-Match': '"4"' } },
    ]);
  });
});
