import { defineStore } from 'pinia';
import { ref } from 'vue';
import {
  createDownloader,
  deleteDownloader,
  diagnoseDownloaderPaths,
  getDownloader,
  getDownloaderMetrics,
  listDownloaders,
  probeDownloader,
  setDownloaderEnabled,
  testDownloader,
  updateDownloader,
  type Downloader,
  type DownloaderCreateInput,
  type DownloaderPatchInput,
  type DownloaderProbeInput,
  type DownloaderRuntimeMetrics,
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
  const metrics = ref<Record<string, DownloaderRuntimeMetrics>>({});
  const metricErrors = ref<Record<string, ApiProblem>>({});

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
        metrics.value = {};
        metricErrors.value = {};
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
        metrics.value = {};
        metricErrors.value = {};
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
      delete metrics.value[item.id];
      delete metricErrors.value[item.id];
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

  async function probe(payload: DownloaderProbeInput) {
    return guarded('probe', () => probeDownloader(payload));
  }

  async function refreshMetrics() {
    const current = [...items.value];
    if (!current.length) {
      metrics.value = {};
      metricErrors.value = {};
      return;
    }
    const results = await Promise.all(
      current.map(async (item) => {
        try {
          return { id: item.id, metric: await getDownloaderMetrics(item.id), problem: null };
        } catch (caught) {
          return { id: item.id, metric: null, problem: toApiProblem(caught) };
        }
      }),
    );
    const unauthorized = results.find((result) => result.problem?.status === 401)?.problem;
    if (unauthorized) {
      error.value = unauthorized;
      items.value = [];
      diagnostics.value = {};
      metrics.value = {};
      metricErrors.value = {};
      return;
    }
    const liveIds = new Set(items.value.map((item) => item.id));
    const nextMetrics = Object.fromEntries(
      Object.entries(metrics.value).filter(([id]) => liveIds.has(id)),
    ) as Record<string, DownloaderRuntimeMetrics>;
    const nextErrors: Record<string, ApiProblem> = {};
    for (const result of results) {
      if (!liveIds.has(result.id)) continue;
      if (result.metric) nextMetrics[result.id] = result.metric;
      else if (result.problem) nextErrors[result.id] = result.problem;
    }
    metrics.value = nextMetrics;
    metricErrors.value = nextErrors;
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
    metrics,
    metricErrors,
    refresh,
    create,
    update,
    remove,
    probe,
    refreshMetrics,
    testConnection,
    diagnose,
    setEnabled,
  };
});
