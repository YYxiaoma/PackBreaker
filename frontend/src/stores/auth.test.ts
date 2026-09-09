import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { ApiProblem } from '../api/client';
import {
  getAuthStatus,
  loginAdministrator,
  logoutAdministrator,
  setupAdministrator,
} from '../api/auth';
import { useAuthStore } from './auth';

vi.mock('../api/auth', async () => {
  const actual = await vi.importActual<typeof import('../api/auth')>('../api/auth');
  return {
    ...actual,
    getAuthStatus: vi.fn(),
    loginAdministrator: vi.fn(),
    logoutAdministrator: vi.fn(),
    setupAdministrator: vi.fn(),
  };
});

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
});

describe('管理员认证 store', () => {
  it('首次初始化后立即建立管理员会话，且 store 不保存口令', async () => {
    vi.mocked(getAuthStatus).mockResolvedValue({
      configured: false,
      authenticated: false,
      permissions: [],
      expires_at: null,
    });
    vi.mocked(setupAdministrator).mockResolvedValue();
    vi.mocked(loginAdministrator).mockResolvedValue({
      authenticated: true,
      expires_at: '2026-09-10T12:00:00Z',
    });
    const store = useAuthStore();
    await store.bootstrap();

    const password = 'synthetic-password-only-for-test';
    await store.setup(password);

    expect(setupAdministrator).toHaveBeenCalledWith(password);
    expect(loginAdministrator).toHaveBeenCalledWith(password);
    expect(store.authenticated).toBe(true);
    expect(store.configured).toBe(true);
    expect(JSON.stringify(store.$state)).not.toContain(password);
  });

  it('登录返回 setup required 时切换回初始化状态', async () => {
    vi.mocked(loginAdministrator).mockRejectedValue(
      new ApiProblem('请先初始化', { status: 409, code: 'AUTH_SETUP_REQUIRED' }),
    );
    const store = useAuthStore();

    await expect(store.login('synthetic-long-password')).rejects.toMatchObject({
      code: 'AUTH_SETUP_REQUIRED',
    });
    expect(store.configured).toBe(false);
    expect(store.authenticated).toBe(false);
  });

  it('退出后立即清除前端认证状态', async () => {
    vi.mocked(loginAdministrator).mockResolvedValue({
      authenticated: true,
      expires_at: '2026-09-10T12:00:00Z',
    });
    vi.mocked(logoutAdministrator).mockResolvedValue();
    const store = useAuthStore();
    await store.login('synthetic-long-password');

    await store.logout();

    expect(store.authenticated).toBe(false);
    expect(store.configured).toBe(true);
  });
});
