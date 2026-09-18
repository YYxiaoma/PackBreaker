import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { ApiProblem } from '../api/client';
import {
  changeAdministratorPassword,
  getAuthStatus,
  loginAdministrator,
  logoutAdministrator,
} from '../api/auth';
import { useAuthStore } from './auth';

vi.mock('../api/auth', async () => {
  const actual = await vi.importActual<typeof import('../api/auth')>('../api/auth');
  return {
    ...actual,
    changeAdministratorPassword: vi.fn(),
    getAuthStatus: vi.fn(),
    loginAdministrator: vi.fn(),
    logoutAdministrator: vi.fn(),
  };
});

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
});

describe('管理员认证 store', () => {
  it('用户名密码登录后建立会话，且 store 不保存口令', async () => {
    vi.mocked(loginAdministrator).mockResolvedValue({
      authenticated: true,
      expires_at: '2026-09-10T12:00:00Z',
      username: 'operator',
      must_change_password: false,
    });
    const store = useAuthStore();

    const password = 'synthetic-password-only-for-test';
    await store.login('operator', password);

    expect(loginAdministrator).toHaveBeenCalledWith('operator', password);
    expect(store.authenticated).toBe(true);
    expect(store.configured).toBe(true);
    expect(store.username).toBe('operator');
    expect(store.mustChangePassword).toBe(false);
    expect(JSON.stringify(store.$state)).not.toContain(password);
  });

  it('登录返回 setup required 时切换回初始化状态', async () => {
    vi.mocked(loginAdministrator).mockRejectedValue(
      new ApiProblem('请先初始化', { status: 409, code: 'AUTH_SETUP_REQUIRED' }),
    );
    const store = useAuthStore();

    await expect(store.login('admin', 'synthetic-long-password')).rejects.toMatchObject({
      code: 'AUTH_SETUP_REQUIRED',
    });
    expect(store.configured).toBe(false);
    expect(store.authenticated).toBe(false);
  });

  it('退出后立即清除前端认证状态', async () => {
    vi.mocked(loginAdministrator).mockResolvedValue({
      authenticated: true,
      expires_at: '2026-09-10T12:00:00Z',
      username: 'admin',
      must_change_password: false,
    });
    vi.mocked(logoutAdministrator).mockResolvedValue();
    const store = useAuthStore();
    await store.login('admin', 'synthetic-long-password');

    await store.logout();

    expect(store.authenticated).toBe(false);
    expect(store.configured).toBe(true);
  });

  it('临时密码会话标记强制改密，改密成功后清除会话状态', async () => {
    vi.mocked(loginAdministrator).mockResolvedValue({
      authenticated: true,
      expires_at: '2026-09-10T12:00:00Z',
      username: 'admin',
      must_change_password: true,
    });
    vi.mocked(changeAdministratorPassword).mockResolvedValue();
    const store = useAuthStore();
    await store.login('admin', 'temporary-synthetic-password');

    expect(store.mustChangePassword).toBe(true);
    await store.changePassword(
      'temporary-synthetic-password',
      'replacement-synthetic-password',
      'replacement-synthetic-password',
    );

    expect(changeAdministratorPassword).toHaveBeenCalledWith({
      current_password: 'temporary-synthetic-password',
      new_password: 'replacement-synthetic-password',
      confirm_password: 'replacement-synthetic-password',
    });
    expect(store.authenticated).toBe(false);
    expect(store.mustChangePassword).toBe(false);
    expect(JSON.stringify(store.$state)).not.toContain('replacement-synthetic-password');
  });
});
