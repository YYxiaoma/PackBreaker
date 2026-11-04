import { describe, expect, it } from 'vitest';
import {
  hasCookieCloudConnectionDraftChanged,
  type CookieCloudConnectionDraft,
  type SavedCookieCloudConnection,
} from './cookiecloudDraft';

const saved: SavedCookieCloudConnection = {
  server_url: 'https://cookie.example.test',
  uuid: 'saved-uuid',
  request_timeout_seconds: 15,
};

const unchangedDraft: CookieCloudConnectionDraft = {
  serverUrl: saved.server_url,
  uuid: saved.uuid,
  password: '',
  clearPassword: false,
  requestTimeoutSeconds: saved.request_timeout_seconds,
};

describe('CookieCloud 当前草稿连接状态', () => {
  it('未修改连接参数时沿用已保存配置的测试状态', () => {
    expect(hasCookieCloudConnectionDraftChanged(unchangedDraft, saved)).toBe(false);
  });

  it.each([
    { field: 'serverUrl', value: 'https://other.example.test' },
    { field: 'uuid', value: 'other-uuid' },
    { field: 'password', value: 'new-password' },
    { field: 'clearPassword', value: true },
    { field: 'requestTimeoutSeconds', value: 3600 },
  ] as const)('修改 $field 后不再沿用旧连接测试状态', ({ field, value }) => {
    const draft = { ...unchangedDraft, [field]: value } as CookieCloudConnectionDraft;
    expect(hasCookieCloudConnectionDraftChanged(draft, saved)).toBe(true);
  });

  it('没有已保存配置时不把空草稿误判为修改过的配置', () => {
    expect(hasCookieCloudConnectionDraftChanged(unchangedDraft, undefined)).toBe(false);
  });
});
