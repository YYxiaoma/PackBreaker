import { apiClient, strongEtag } from './client';
import type { components } from './generated/schema';

export type Site = components['schemas']['SiteViewResponse'];
export type SiteKind = components['schemas']['SiteKind'];
export type SiteCredentialKind = components['schemas']['SiteCredentialKind'];
export type SiteCredentialInput = components['schemas']['SiteCredentialInput'];
export type SiteCreateInput = components['schemas']['SiteCreateRequest'];
export type SitePatchInput = components['schemas']['SitePatchRequest'];
export type SiteProbeResult = components['schemas']['SiteProbeResponse'];
export type SiteHealth = components['schemas']['SiteHealthResponse'];

export function credentialKindForSite(kind: SiteKind): SiteCredentialKind {
  return kind === 'MTEAM' ? 'API_KEY' : 'COOKIE';
}

function sitePath(id: string): string {
  const normalized = id.trim();
  if (!normalized) throw new Error('siteId 不能为空');
  return `/sites/${encodeURIComponent(normalized)}`;
}

export async function listSites(): Promise<Site[]> {
  const response = await apiClient.get<{ items: Site[] }>('/sites');
  return response.data.items;
}

export async function getSite(id: string): Promise<Site> {
  const response = await apiClient.get<Site>(sitePath(id));
  return response.data;
}

export async function createSite(payload: SiteCreateInput): Promise<Site> {
  const response = await apiClient.post<Site>('/sites', payload);
  return response.data;
}

export async function updateSite(
  id: string,
  version: number,
  payload: SitePatchInput,
): Promise<Site> {
  const response = await apiClient.patch<Site>(sitePath(id), payload, {
    headers: { 'If-Match': strongEtag(version) },
  });
  return response.data;
}

export async function deleteSite(id: string, version: number): Promise<void> {
  await apiClient.delete(sitePath(id), {
    headers: { 'If-Match': strongEtag(version) },
  });
}

export async function testSite(id: string): Promise<SiteProbeResult> {
  const response = await apiClient.post<SiteProbeResult>(`${sitePath(id)}/test`);
  return response.data;
}

export async function getSiteHealth(id: string): Promise<SiteHealth> {
  const response = await apiClient.get<SiteHealth>(`${sitePath(id)}/health`);
  return response.data;
}

export async function setSiteEnabled(id: string, version: number, enabled: boolean): Promise<Site> {
  const response = await apiClient.post<Site>(
    `${sitePath(id)}/actions`,
    { action: enabled ? 'enable' : 'disable' },
    { headers: { 'If-Match': strongEtag(version) } },
  );
  return response.data;
}

export async function resetSiteCircuit(id: string, version: number): Promise<SiteHealth> {
  const response = await apiClient.post<SiteHealth>(
    `${sitePath(id)}/actions`,
    { action: 'reset_circuit' },
    { headers: { 'If-Match': strongEtag(version) } },
  );
  return response.data;
}
