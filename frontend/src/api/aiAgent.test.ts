import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiClient } from './client';
import {
  getAITelegramBinding,
  probeAIAgent,
  updateAITelegramBinding,
  updateAIAgentSettings,
  type AIAgentSettings,
  type AIAgentSettingUpdate,
  type AITelegramBinding,
} from './aiAgent';

const settings: AIAgentSettings = {
  enabled: false,
  provider_kind: 'OPENAI_COMPATIBLE',
  base_url: 'https://provider.example/v1',
  api_key_configured: true,
  model: 'synthetic-model',
  request_timeout_seconds: 30,
  max_context_messages: 20,
  data_scopes: ['SYSTEM_HEALTH'],
  connection_status: 'OK',
  last_test_at: '2026-09-17T00:00:00Z',
  version: 4,
  created_at: '2026-09-17T00:00:00Z',
  updated_at: '2026-09-17T00:00:00Z',
};

const telegramBinding: AITelegramBinding = {
  notification_channel_id: 'telegram-channel',
  enabled: true,
  approval_enabled: true,
  allowed_chat_ids: ['-100123'],
  allowed_user_ids: ['88'],
  idle_timeout_minutes: 60,
  max_context_messages: 20,
  last_update_id: 42,
  version: 3,
  created_at: '2026-09-17T00:00:00Z',
  updated_at: '2026-09-17T00:00:00Z',
};

afterEach(() => vi.restoreAllMocks());

describe('AI Agent API', () => {
  it('更新设置携带强 If-Match', async () => {
    const put = vi.spyOn(apiClient, 'put').mockResolvedValue({ data: settings });
    const payload: AIAgentSettingUpdate = {
      enabled: false,
      provider_kind: 'OPENAI_COMPATIBLE',
      base_url: 'https://provider.example/v1',
      model: 'synthetic-model',
      request_timeout_seconds: 30,
      max_context_messages: 20,
      data_scopes: ['SYSTEM_HEALTH'],
      api_key_action: 'KEEP',
    };

    await updateAIAgentSettings(settings, payload);

    expect(put).toHaveBeenCalledWith('/ai-agent/settings', payload, {
      headers: { 'If-Match': '"4"' },
    });
  });

  it('未保存测试使用独立 probe 且明确 use_saved=false', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: {
        status: 'ok',
        provider_kind: 'OPENAI_COMPATIBLE',
        model: 'synthetic-model',
        tested_at: '2026-09-17T00:00:00Z',
      },
    });
    const payload = {
      use_saved: false,
      provider_kind: 'OPENAI_COMPATIBLE' as const,
      base_url: 'https://provider.example/v1',
      model: 'synthetic-model',
      request_timeout_seconds: 30,
      api_key: 'synthetic-key',
    };

    await probeAIAgent(payload);

    expect(post).toHaveBeenCalledWith('/ai-agent/test', payload);
  });

  it('读取 Telegram AI 绑定使用独立配置接口', async () => {
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: telegramBinding });

    await getAITelegramBinding();

    expect(get).toHaveBeenCalledWith('/ai-agent/telegram');
  });

  it('更新 Telegram AI 绑定携带强 If-Match', async () => {
    const put = vi.spyOn(apiClient, 'put').mockResolvedValue({ data: telegramBinding });
    const payload = {
      notification_channel_id: 'telegram-channel',
      enabled: true,
      approval_enabled: true,
      allowed_chat_ids: ['-100123'],
      allowed_user_ids: ['88'],
      idle_timeout_minutes: 60,
      max_context_messages: 20,
    };

    await updateAITelegramBinding(telegramBinding, payload);

    expect(put).toHaveBeenCalledWith('/ai-agent/telegram', payload, {
      headers: { 'If-Match': '"3"' },
    });
  });
});
