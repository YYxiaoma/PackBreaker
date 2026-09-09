import { defineStore } from 'pinia';
import { ref } from 'vue';
import {
  createDownloader,
  deleteDownloader,
  diagnoseDownloaderPaths,
  getDownloader,
  listDownloaders,
  setDownloaderEnabled,
  testDownloader,
  updateDownloader,
  type Downloader,
  type DownloaderCreateInput,
  type DownloaderPatchInput,
  type PathDiagnosticProbeInput,
  type PathDiagnosticReport,
} from '../api/downloaders';
import { ApiProblem, toApiProblem } from '../api/client';

export const useDownloaderStore = defineStore('downloaders', () => {
  const items = ref<Downloader[]>([]);
  const loading = ref(false);
  const error = ref<ApiProblem | null>(null);
  const busy = ref<Record<string, boolean>>({});
  const diagnostics = ref<Record<string, PathDiagnosticReport>>({});

  function replace(item: Downloader) {
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
        diagnostics.value = {};
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
      items.value = await listDownloaders();
      error.value = null;
    } catch (caught) {
      const problem = toApiProblem(caught);
      error.value = problem;
      if (problem.status === 401) {
        items.value = [];
        diagnostics.value = {};
      }
      throw problem;
    } finally {
      loading.value = false;
    }
  }

  async function create(payload: DownloaderCreateInput) {
    return guarded('create', async () => {
      const created = await createDownloader(payload);
      replace(created);
      return created;
    });
  }

  async function update(item: Downloader, payload: DownloaderPatchInput) {
    return guarded(`update:${item.id}`, async () => {
      const updated = await updateDownloader(item.id, item.version, payload);
      replace(updated);
      return updated;
    });
  }

  async function remove(item: Downloader) {
    return guarded(`delete:${item.id}`, async () => {
      await deleteDownloader(item.id, item.version);
      items.value = items.value.filter((current) => current.id !== item.id);
      delete diagnostics.value[item.id];
    });
  }

  async function refreshOne(id: string) {
    const current = await getDownloader(id);
    replace(current);
    return current;
  }

  async function testConnection(item: Downloader) {
    return guarded(`test:${item.id}`, async () => {
      const result = await testDownloader(item.id);
      await refreshOne(item.id);
      return result;
    });
  }

  async function diagnose(item: Downloader, probes: PathDiagnosticProbeInput[]) {
    return guarded(`diagnose:${item.id}`, async () => {
      const report = await diagnoseDownloaderPaths(item.id, probes);
      diagnostics.value = { ...diagnostics.value, [item.id]: report };
      await refreshOne(item.id);
      return report;
    });
  }

  async function setEnabled(item: Downloader, enabled: boolean) {
    return guarded(`enable:${item.id}`, async () => {
      const updated = await setDownloaderEnabled(item.id, item.version, enabled);
      replace(updated);
      return updated;
    });
  }

  return {
    items,
    loading,
    error,
    busy,
    diagnostics,
    refresh,
    create,
    update,
    remove,
    testConnection,
    diagnose,
    setEnabled,
  };
});
