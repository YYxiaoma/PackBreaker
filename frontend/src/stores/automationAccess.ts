import { ref } from 'vue';
import { defineStore } from 'pinia';
import { ApiProblem, toApiProblem } from '../api/client';
import {
  createApiToken,
  listApiTokens,
  revokeApiToken,
  type ApiTokenCreateInput,
  type ApiTokenCreated,
  type ApiTokenView,
} from '../api/automationAccess';

export const useApiTokenStore = defineStore('apiTokens', () => {
  const items = ref<ApiTokenView[]>([]);
  const loading = ref(false);
  const busy = ref(false);
  const error = ref<ApiProblem | null>(null);

  async function refresh(): Promise<void> {
    loading.value = true;
    try {
      items.value = await listApiTokens();
      error.value = null;
    } catch (caught) {
      error.value = toApiProblem(caught);
      throw error.value;
    } finally {
      loading.value = false;
    }
  }

  async function create(payload: ApiTokenCreateInput): Promise<ApiTokenCreated> {
    busy.value = true;
    try {
      const created = await createApiToken(payload);
      items.value.unshift({
        id: created.id,
        name: created.name,
        scopes: created.scopes,
        expires_at: created.expires_at,
        revoked_at: null,
        created_at: created.created_at,
      });
      error.value = null;
      return created;
    } catch (caught) {
      error.value = toApiProblem(caught);
      throw error.value;
    } finally {
      busy.value = false;
    }
  }

  async function revoke(tokenId: string): Promise<void> {
    busy.value = true;
    try {
      await revokeApiToken(tokenId);
      await refresh();
      error.value = null;
    } catch (caught) {
      error.value = toApiProblem(caught);
      throw error.value;
    } finally {
      busy.value = false;
    }
  }

  function clear(): void {
    items.value = [];
    error.value = null;
  }

  return { items, loading, busy, error, refresh, create, revoke, clear };
});
