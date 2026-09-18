import { computed, ref } from 'vue';
import { defineStore } from 'pinia';
import { ApiProblem, toApiProblem } from '../api/client';
import {
  changeAdministratorPassword,
  getAuthStatus,
  loginAdministrator,
  logoutAdministrator,
  type AuthStatus,
} from '../api/auth';

function anonymousStatus(configured: boolean): AuthStatus {
  return {
    configured,
    authenticated: false,
    permissions: [],
    expires_at: null,
    username: null,
    must_change_password: false,
  };
}

export const useAuthStore = defineStore('auth', () => {
  const status = ref<AuthStatus | null>(null);
  const loading = ref(true);
  const submitting = ref(false);
  const error = ref<ApiProblem | null>(null);

  const configured = computed(() => status.value?.configured ?? false);
  const authenticated = computed(() => status.value?.authenticated ?? false);
  const expiresAt = computed(() => status.value?.expires_at ?? null);
  const username = computed(() => status.value?.username ?? null);
  const mustChangePassword = computed(() => status.value?.must_change_password ?? false);

  async function bootstrap(): Promise<void> {
    loading.value = true;
    try {
      status.value = await getAuthStatus();
      error.value = null;
    } catch (caught) {
      error.value = toApiProblem(caught);
    } finally {
      loading.value = false;
    }
  }

  async function login(username: string, password: string): Promise<void> {
    submitting.value = true;
    try {
      const result = await loginAdministrator(username, password);
      status.value = {
        configured: true,
        authenticated: true,
        permissions: result.must_change_password ? ['password:change'] : ['admin'],
        expires_at: result.expires_at,
        username: result.username,
        must_change_password: result.must_change_password,
      };
      error.value = null;
    } catch (caught) {
      const problem = toApiProblem(caught);
      if (problem.code === 'AUTH_SETUP_REQUIRED') status.value = anonymousStatus(false);
      error.value = problem;
      throw problem;
    } finally {
      submitting.value = false;
    }
  }

  async function changePassword(
    currentPassword: string,
    newPassword: string,
    confirmation: string,
  ): Promise<void> {
    submitting.value = true;
    try {
      await changeAdministratorPassword({
        current_password: currentPassword,
        new_password: newPassword,
        confirm_password: confirmation,
      });
      status.value = anonymousStatus(true);
      error.value = null;
    } catch (caught) {
      const problem = toApiProblem(caught);
      error.value = problem;
      throw problem;
    } finally {
      submitting.value = false;
    }
  }

  async function logout(): Promise<void> {
    submitting.value = true;
    try {
      await logoutAdministrator();
      error.value = null;
    } finally {
      status.value = anonymousStatus(true);
      submitting.value = false;
    }
  }

  function markUnauthenticated(): void {
    status.value = anonymousStatus(status.value?.configured ?? true);
  }

  return {
    status,
    loading,
    submitting,
    error,
    configured,
    authenticated,
    expiresAt,
    username,
    mustChangePassword,
    bootstrap,
    login,
    changePassword,
    logout,
    markUnauthenticated,
  };
});
