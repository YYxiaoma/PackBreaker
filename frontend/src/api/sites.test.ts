import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiClient } from './client';
import {
  credentialKindForSite,
  deleteSite,
  resetSiteCircuit,
  setSiteEnabled,
  updateSite,
} from './sites';

afterEach(() => vi.restoreAllMocks());

describe('站点 API 并发前置条件', () => {
  it('站点类型只映射到各自允许的凭证类型', () => {
    expect(credentialKindForSite('MTEAM')).toBe('API_KEY');
    expect(credentialKindForSite('HDTIME')).toBe('COOKIE');
    expect(credentialKindForSite('ROUSI_PRO')).toBe('API_KEY');
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

  it('删除、启停与 reset-circuit 都携带当前版本', async () => {
    const remove = vi.spyOn(apiClient, 'delete').mockResolvedValue({ data: undefined });
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({ data: {} });

    await deleteSite('site-1', 4);
    await setSiteEnabled('site-1', 4, true);
    await resetSiteCircuit('site-1', 4);

    expect(remove).toHaveBeenCalledWith('/sites/site-1', {
      headers: { 'If-Match': '"4"' },
    });
    expect(post.mock.calls[0]).toEqual([
      '/sites/site-1/actions',
      { action: 'enable' },
      { headers: { 'If-Match': '"4"' } },
    ]);
    expect(post.mock.calls[1]).toEqual([
      '/sites/site-1/actions',
      { action: 'reset_circuit' },
      { headers: { 'If-Match': '"4"' } },
    ]);
  });
});
