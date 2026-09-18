import { apiClient, strongEtag, toApiProblem } from './client';
import type { components } from './generated/schema';

export type AIAgentSettings = components['schemas']['AIAgentSettingResponse'];
export type AIAgentSettingUpdate = components['schemas']['AIAgentSettingUpdateRequest'];
export type AIAgentProbeRequest = components['schemas']['AIAgentProbeRequest'];
export type AIAgentProbeResponse = components['schemas']['AIAgentProbeResponse'];
export type AIAgentStatus = components['schemas']['AIAgentStatusResponse'];
export type AITelegramBinding = components['schemas']['AITelegramBindingResponse'];
export type AITelegramBindingUpdate = components['schemas']['AITelegramBindingUpdateRequest'];
export type AIProviderKind = components['schemas']['AIProviderKind'];
export type AIDataScope = components['schemas']['AIDataScope'];

export async function getAIAgentSettings(): Promise<AIAgentSettings> {
  try {
    const response = await apiClient.get<AIAgentSettings>('/ai-agent/settings');
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function updateAIAgentSettings(
  current: AIAgentSettings,
  payload: AIAgentSettingUpdate,
): Promise<AIAgentSettings> {
  try {
    const response = await apiClient.put<AIAgentSettings>('/ai-agent/settings', payload, {
      headers: { 'If-Match': strongEtag(current.version) },
    });
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function probeAIAgent(payload: AIAgentProbeRequest): Promise<AIAgentProbeResponse> {
  try {
    const response = await apiClient.post<AIAgentProbeResponse>('/ai-agent/test', payload);
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function getAIAgentStatus(): Promise<AIAgentStatus> {
  try {
    const response = await apiClient.get<AIAgentStatus>('/ai-agent/status');
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function getAITelegramBinding(): Promise<AITelegramBinding> {
  try {
    const response = await apiClient.get<AITelegramBinding>('/ai-agent/telegram');
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function updateAITelegramBinding(
  current: AITelegramBinding,
  payload: AITelegramBindingUpdate,
): Promise<AITelegramBinding> {
  try {
    const response = await apiClient.put<AITelegramBinding>('/ai-agent/telegram', payload, {
      headers: { 'If-Match': strongEtag(current.version) },
    });
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}
