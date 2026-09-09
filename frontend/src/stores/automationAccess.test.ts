import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { createApiToken, listApiTokens, revokeApiToken } from '../api/automationAccess';
import { useApiTokenStore } from './automationAccess';

vi.mock('../api/automationAccess', async () => {
  const actual =
    await vi.importActual<typeof import('../api/automationAccess')>('../api/automationAccess');
  return {
    ...actual,
    createApiToken: vi.fn(),
    listApiTokens: vi.fn(),
    revokeApiToken: vi.fn(),
  };
});

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
});

describe('API Token store', () => {
  it('创建结果只把脱敏元数据写入 store，不保留明文 Token', async () => {
    const plaintext = 'pbk_SYNTHETIC-PLAINTEXT-ONLY-ONCE';
    vi.mocked(createApiToken).mockResolvedValue({
      id: 'token-001',
      name: 'readonly',
      token: plaintext,
      scopes: ['config:read'],
      expires_at: '2026-10-09T00:00:00Z',
      created_at: '2026-09-09T00:00:00Z',
    });
    const store = useApiTokenStore();

    const created = await store.create({
      name: 'readonly',
      scopes: ['config:read'],
      expires_at: '2026-10-09T00:00:00Z',
    });

    expect(created.token).toBe(plaintext);
    expect(store.items[0]).not.toHaveProperty('token');
    expect(JSON.stringify(store.$state)).not.toContain(plaintext);
  });

  it('撤销后重新读取后端状态，不在前端伪造撤销时间', async () => {
    vi.mocked(revokeApiToken).mockResolvedValue();
    vi.mocked(listApiTokens).mockResolvedValue([
      {
        id: 'token-001',
        name: 'readonly',
        scopes: ['config:read'],
        expires_at: '2026-10-09T00:00:00Z',
        revoked_at: '2026-09-09T12:00:00Z',
        created_at: '2026-09-09T00:00:00Z',
      },
    ]);
    const store = useApiTokenStore();

    await store.revoke('token-001');

    expect(revokeApiToken).toHaveBeenCalledWith('token-001');
    expect(listApiTokens).toHaveBeenCalledOnce();
    expect(store.items[0]?.revoked_at).toBe('2026-09-09T12:00:00Z');
  });
});
