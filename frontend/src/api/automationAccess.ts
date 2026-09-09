import { apiClient, toApiProblem } from './client';
import type { components } from './generated/schema';

export type ApiScope = components['schemas']['ApiScope'];
export type ApiTokenCreateInput = components['schemas']['ApiTokenCreateRequest'];
export type ApiTokenCreated = components['schemas']['ApiTokenCreatedResponse'];
export type ApiTokenView = components['schemas']['ApiTokenViewResponse'];
export type ApiTokenList = components['schemas']['ApiTokenListResponse'];

export async function listApiTokens(): Promise<ApiTokenView[]> {
  try {
    const response = await apiClient.get<ApiTokenList>('/api-tokens');
    return response.data.items;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function createApiToken(payload: ApiTokenCreateInput): Promise<ApiTokenCreated> {
  try {
    const response = await apiClient.post<ApiTokenCreated>('/api-tokens', payload);
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function revokeApiToken(tokenId: string): Promise<void> {
  try {
    await apiClient.delete(`/api-tokens/${encodeURIComponent(tokenId)}`);
  } catch (error) {
    throw toApiProblem(error);
  }
}
