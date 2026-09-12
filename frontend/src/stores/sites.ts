import { defineStore } from 'pinia';
import { ref } from 'vue';

import { ApiProblem, toApiProblem } from '../api/client';
import {
  createSite,
  deleteSite,
  getSite,
  getSiteHealth,
  listSites,
  resetSiteCircuit,
  setSiteEnabled,
  testSite,
  updateSite,
  type Site,
  type SiteCreateInput,
  type SiteHealth,
  type SitePatchInput,
} from '../api/sites';

export const useSiteStore = defineStore('sites', () => {
  const items = ref<Site[]>([]);
  const health = ref<Record<string, SiteHealth>>({});
  const loading = ref(false);
  const error = ref<ApiProblem | null>(null);
  const busy = ref<Record<string, boolean>>({});

  function replace(item: Site) {
    const index = items.value.findIndex((current) => current.id === item.id);
    if (index >= 0) items.value[index] = item;
    else items.value.unshift(item);
  }

  function clearSiteState(id: string) {
    const next = { ...health.value };
    delete next[id];
    health.value = next;
  }

  async function guarded<T>(key: string, action: () => Promise<T>): Promise<T> {
    busy.value = { ...busy.value, [key]: true };
    try {
      const result = await action();
      error.value = null;
      return result;
    } catch (caught) {
      const problem = toApiProblem(caught);
      error.value = problem;
      if (problem.status === 401) {
        items.value = [];
        health.value = {};
      }
      throw problem;
    } finally {
      const next = { ...busy.value };
      delete next[key];
      busy.value = next;
    }
  }

  async function refreshHealth(item: Site): Promise<SiteHealth | null> {
    try {
      const result = await getSiteHealth(item.id);
      health.value = { ...health.value, [item.id]: result };
      return result;
    } catch (caught) {
      const problem = toApiProblem(caught);
      if (problem.status === 401) throw problem;
      clearSiteState(item.id);
      return null;
    }
  }

  async function refresh() {
    loading.value = true;
    try {
      items.value = await listSites();
      error.value = null;
      const knownIds = new Set(items.value.map((item) => item.id));
      health.value = Object.fromEntries(
        Object.entries(health.value).filter(([siteId]) => knownIds.has(siteId)),
      );
      await Promise.all(items.value.map((item) => refreshHealth(item)));
    } catch (caught) {
      const problem = toApiProblem(caught);
      error.value = problem;
      if (problem.status === 401) {
        items.value = [];
        health.value = {};
      }
      throw problem;
    } finally {
      loading.value = false;
    }
  }

  async function refreshOne(id: string): Promise<Site> {
    const current = await getSite(id);
    replace(current);
    await refreshHealth(current);
    return current;
  }

  async function create(payload: SiteCreateInput) {
    return guarded('create', async () => {
      const created = await createSite(payload);
      replace(created);
      await refreshHealth(created);
      return created;
    });
  }

  async function update(item: Site, payload: SitePatchInput) {
    return guarded(`update:${item.id}`, async () => {
      const updated = await updateSite(item.id, item.version, payload);
      replace(updated);
      await refreshHealth(updated);
      return updated;
    });
  }

  async function remove(item: Site) {
    return guarded(`delete:${item.id}`, async () => {
      await deleteSite(item.id, item.version);
      items.value = items.value.filter((current) => current.id !== item.id);
      clearSiteState(item.id);
    });
  }

  async function testConnection(item: Site) {
    return guarded(`test:${item.id}`, async () => {
      const result = await testSite(item.id);
      await refreshOne(item.id);
      return result;
    });
  }

  async function setEnabled(item: Site, enabled: boolean) {
    return guarded(`enable:${item.id}`, async () => {
      const updated = await setSiteEnabled(item.id, item.version, enabled);
      replace(updated);
      await refreshHealth(updated);
      return updated;
    });
  }

  async function resetCircuit(item: Site) {
    return guarded(`reset:${item.id}`, async () => {
      const result = await resetSiteCircuit(item.id, item.version);
      health.value = { ...health.value, [item.id]: result };
      return result;
    });
  }

  return {
    items,
    health,
    loading,
    error,
    busy,
    refresh,
    create,
    update,
    remove,
    testConnection,
    setEnabled,
    resetCircuit,
  };
});
