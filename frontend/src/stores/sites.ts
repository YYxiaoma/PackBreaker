import { defineStore } from 'pinia';
import { ref } from 'vue';

import { ApiProblem, toApiProblem } from '../api/client';
import {
  createSite,
  deleteSite,
  getSite,
  listSiteProfiles,
  listSites,
  probeSite,
  setSiteEnabled,
  testSite,
  updateSite,
  type Site,
  type SiteCreateInput,
  type SitePatchInput,
  type SiteProfile,
  type SiteTemporaryProbeInput,
} from '../api/sites';

export const useSiteStore = defineStore('sites', () => {
  const items = ref<Site[]>([]);
  const profiles = ref<SiteProfile[]>([]);
  const loading = ref(false);
  const error = ref<ApiProblem | null>(null);
  const busy = ref<Record<string, boolean>>({});

  function replace(item: Site) {
    const index = items.value.findIndex((current) => current.id === item.id);
    if (index >= 0) items.value[index] = item;
    else items.value.unshift(item);
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
        profiles.value = [];
      }
      throw problem;
    } finally {
      const next = { ...busy.value };
      delete next[key];
      busy.value = next;
    }
  }

  async function refresh() {
    loading.value = true;
    try {
      const [siteItems, siteProfiles] = await Promise.all([listSites(), listSiteProfiles()]);
      items.value = siteItems;
      profiles.value = siteProfiles;
      error.value = null;
    } catch (caught) {
      const problem = toApiProblem(caught);
      error.value = problem;
      if (problem.status === 401) {
        items.value = [];
        profiles.value = [];
      }
      throw problem;
    } finally {
      loading.value = false;
    }
  }

  async function refreshOne(id: string): Promise<Site> {
    const current = await getSite(id);
    replace(current);
    return current;
  }

  async function create(payload: SiteCreateInput) {
    return guarded('create', async () => {
      const created = await createSite(payload);
      replace(created);
      return created;
    });
  }

  async function update(item: Site, payload: SitePatchInput) {
    return guarded(`update:${item.id}`, async () => {
      const updated = await updateSite(item.id, item.version, payload);
      replace(updated);
      return updated;
    });
  }

  async function remove(item: Site) {
    return guarded(`delete:${item.id}`, async () => {
      await deleteSite(item.id, item.version);
      items.value = items.value.filter((current) => current.id !== item.id);
    });
  }

  async function testConnection(item: Site) {
    return guarded(`test:${item.id}`, async () => {
      const result = await testSite(item.id);
      await refreshOne(item.id);
      return result;
    });
  }

  async function probeTemporary(payload: SiteTemporaryProbeInput) {
    return guarded('probe', () => probeSite(payload));
  }

  async function setEnabled(item: Site, enabled: boolean) {
    return guarded(`enable:${item.id}`, async () => {
      const updated = await setSiteEnabled(item.id, item.version, enabled);
      replace(updated);
      return updated;
    });
  }

  return {
    items,
    profiles,
    loading,
    error,
    busy,
    refresh,
    create,
    update,
    remove,
    testConnection,
    probeTemporary,
    setEnabled,
  };
});
