import { apiClient, strongEtag, toApiProblem } from './client';
import type { components } from './generated/schema';

export type CookieCloudSettings = components['schemas']['CookieCloudSettingResponse'];
export type CookieCloudSettingUpdate = components['schemas']['CookieCloudSettingUpdateRequest'];
export type CookieCloudProbe = components['schemas']['CookieCloudProbeResponse'];
export type CookieCloudSyncReport = components['schemas']['CookieCloudSyncResponse'];
export type CookieCloudCronPreview = components['schemas']['CookieCloudCronPreviewResponse'];

export async function getCookieCloudSettings(): Promise<CookieCloudSettings> {
  try {
    const response = await apiClient.get<CookieCloudSettings>('/cookiecloud/config');
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function updateCookieCloudSettings(
  current: CookieCloudSettings,
  payload: CookieCloudSettingUpdate,
): Promise<CookieCloudSettings> {
  try {
    const response = await apiClient.put<CookieCloudSettings>('/cookiecloud/config', payload, {
      headers: { 'If-Match': strongEtag(current.version) },
    });
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function testCookieCloud(): Promise<CookieCloudProbe> {
  try {
    const response = await apiClient.post<CookieCloudProbe>('/cookiecloud/test');
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function syncCookieCloud(): Promise<CookieCloudSyncReport> {
  try {
    const response = await apiClient.post<CookieCloudSyncReport>('/cookiecloud/sync');
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function previewCookieCloudCron(
  cronExpression: string,
): Promise<CookieCloudCronPreview> {
  try {
    const response = await apiClient.post<CookieCloudCronPreview>('/cookiecloud/cron-preview', {
      cron_expression: cronExpression,
    });
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}
