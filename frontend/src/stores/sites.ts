import { defineStore } from 'pinia';
import { ref } from 'vue';

import { ApiProblem, toApiProblem } from '../api/client';
import {
  createSite,
  deleteSite,
  getSite,
  getSiteHealth,
  getSiteUserProfile,
  listSiteProfiles,
  listSites,
  probeSite,
  resetSiteCircuit,
  setSiteEnabled,
  testSite,
  updateSite,
  type Site,
  type SiteCreateInput,
  type SiteHealth,
  type SitePatchInput,
  type SiteProfile,
  type SiteTemporaryProbeInput,
  type SiteUserProfile,
} from '../api/sites';

export const useSiteStore = defineStore('sites', () => {
  const items = ref<Site[]>([]);
  const profiles = ref<SiteProfile[]>([]);
  const health = ref<Record<string, SiteHealth>>({});
  const userProfiles = ref<Record<string, SiteUserProfile>>({});
  const userProfileErrors = ref<Record<string, ApiProblem>>({});
  const loading = ref(false);
  const error = ref<ApiProblem | null>(null);
  const busy = ref<Record<string, boolean>>({});

  // A response started under an earlier configuration may not restore private
  // profile data after the site has been updated, removed, or refreshed.
  const profileRequestTokens = new Map<string, number>();
  let nextProfileToken = 0;
  const healthRequestTokens = new Map<string, number>();
  let nextHealthToken = 0;

  function eraseProfileDisplay(id: string) {
    const nextProfiles = { ...userProfiles.value };
    delete nextProfiles[id];
    userProfiles.value = nextProfiles;
    const nextErrors = { ...userProfileErrors.value };
    delete nextErrors[id];
    userProfileErrors.value = nextErrors;
  }

  function invalidateProfile(id: string) {
    profileRequestTokens.set(id, ++nextProfileToken);
    eraseProfileDisplay(id);
    const nextBusy = { ...busy.value };
    delete nextBusy[`profile:${id}`];
    busy.value = nextBusy;
  }

  function resetPrivateProfileState() {
    for (const id of profileRequestTokens.keys()) profileRequestTokens.set(id, ++nextProfileToken);
    userProfiles.value = {};
    userProfileErrors.value = {};
    busy.value = Object.fromEntries(
      Object.entries(busy.value).filter(([key]) => !key.startsWith('profile:')),
    );
  }

  function replace(item: Site) {
    const index = items.value.findIndex((current) => current.id === item.id);
    if (index >= 0) {
      if (items.value[index]?.version !== item.version) {
        invalidateProfile(item.id);
        invalidateHealth(item.id);
      }
      items.value[index] = item;
    } else items.value.unshift(item);
  }

  function clearHealthState(id: string) {
    const next = { ...health.value };
    delete next[id];
    health.value = next;
  }

  function invalidateHealth(id: string) {
    healthRequestTokens.set(id, ++nextHealthToken);
    clearHealthState(id);
  }

  function clearSiteState(id: string) {
    invalidateHealth(id);
    invalidateProfile(id);
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
        health.value = {};
        resetPrivateProfileState();
      }
      throw problem;
    } finally {
      const next = { ...busy.value };
      delete next[key];
      busy.value = next;
    }
  }

  async function refreshHealth(item: Site): Promise<SiteHealth | null> {
    const token = ++nextHealthToken;
    healthRequestTokens.set(item.id, token);
    const isCurrent = () =>
      healthRequestTokens.get(item.id) === token &&
      items.value.some((current) => current.id === item.id && current.version === item.version);
    try {
      const result = await getSiteHealth(item.id);
      if (!isCurrent()) return null;
      if (result.config_version !== item.version) {
        clearHealthState(item.id);
        return null;
      }
      health.value = { ...health.value, [item.id]: result };
      return result;
    } catch (caught) {
      const problem = toApiProblem(caught);
      if (!isCurrent()) return null;
      if (problem.status === 401) throw problem;
      clearHealthState(item.id);
      return null;
    }
  }

  async function refresh() {
    loading.value = true;
    try {
      const [siteItems, siteProfiles] = await Promise.all([listSites(), listSiteProfiles()]);
      for (const previous of items.value) {
        const current = siteItems.find((item) => item.id === previous.id);
        if (!current || current.version !== previous.version) {
          invalidateProfile(previous.id);
          invalidateHealth(previous.id);
        }
      }
      items.value = siteItems;
      profiles.value = siteProfiles;
      error.value = null;
      const knownIds = new Set(items.value.map((item) => item.id));
      health.value = Object.fromEntries(
        Object.entries(health.value).filter(([siteId]) => knownIds.has(siteId)),
      );
      userProfiles.value = Object.fromEntries(
        Object.entries(userProfiles.value).filter(([siteId]) => knownIds.has(siteId)),
      );
      userProfileErrors.value = Object.fromEntries(
        Object.entries(userProfileErrors.value).filter(([siteId]) => knownIds.has(siteId)),
      );
      await Promise.all(items.value.map((item) => refreshHealth(item)));
    } catch (caught) {
      const problem = toApiProblem(caught);
      error.value = problem;
      if (problem.status === 401) {
        items.value = [];
        profiles.value = [];
        health.value = {};
        resetPrivateProfileState();
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

  async function probeTemporary(payload: SiteTemporaryProbeInput) {
    return guarded('probe', () => probeSite(payload));
  }

  async function loadUserProfile(item: Site): Promise<SiteUserProfile> {
    const key = `profile:${item.id}`;
    const token = ++nextProfileToken;
    profileRequestTokens.set(item.id, token);
    // Explicit refresh is a new read: never display stale personal statistics
    // as if they were the result of the current request.
    eraseProfileDisplay(item.id);
    busy.value = { ...busy.value, [key]: true };
    const isCurrent = () =>
      profileRequestTokens.get(item.id) === token &&
      items.value.some((current) => current.id === item.id && current.version === item.version);
    try {
      const result = await getSiteUserProfile(item.id);
      if (isCurrent()) userProfiles.value = { ...userProfiles.value, [item.id]: result };
      return result;
    } catch (caught) {
      const problem = toApiProblem(caught);
      if (isCurrent()) userProfileErrors.value = { ...userProfileErrors.value, [item.id]: problem };
      if (problem.status === 401 && isCurrent()) {
        items.value = [];
        profiles.value = [];
        health.value = {};
        resetPrivateProfileState();
      }
      throw problem;
    } finally {
      if (profileRequestTokens.get(item.id) === token) {
        const next = { ...busy.value };
        delete next[key];
        busy.value = next;
      }
    }
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
      const current = items.value.find((site) => site.id === item.id);
      if (current?.version === item.version && result.config_version === current.version) {
        // A reset requested against an earlier config must not hide the newer
        // config's OPEN / degraded circuit state when its response arrives late.
        invalidateHealth(item.id);
        health.value = { ...health.value, [item.id]: result };
      }
      return result;
    });
  }

  return {
    items,
    profiles,
    health,
    userProfiles,
    userProfileErrors,
    loading,
    error,
    busy,
    refresh,
    refreshHealth,
    create,
    update,
    remove,
    testConnection,
    probeTemporary,
    loadUserProfile,
    setEnabled,
    resetCircuit,
  };
});
