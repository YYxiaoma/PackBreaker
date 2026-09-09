import { describe, expect, it } from 'vitest';
import { ApiProblem, strongEtag, toApiProblem } from './client';

describe('API 安全错误映射', () => {
  it('只保留 problem+json 的脱敏字段，不回显请求配置或凭证', () => {
    const problem = toApiProblem({
      isAxiosError: true,
      config: { data: '{"password":"PACKBREAKER-CLIENT-SECRET"}' },
      response: {
        status: 412,
        data: {
          title: '版本冲突',
          detail: '下载器配置已经变化，请刷新后重试',
          code: 'DOWNLOADER_VERSION_CONFLICT',
          trace_id: 'trace-synthetic-001',
        },
      },
    });

    expect(problem).toBeInstanceOf(ApiProblem);
    expect(problem.status).toBe(412);
    expect(problem.code).toBe('DOWNLOADER_VERSION_CONFLICT');
    expect(problem.traceId).toBe('trace-synthetic-001');
    expect(problem.message).toContain('请刷新后重试');
    expect(problem.message).not.toContain('PACKBREAKER-CLIENT-SECRET');
  });

  it('网络错误返回固定错误，不泄漏底层请求信息', () => {
    const problem = toApiProblem({
      isAxiosError: true,
      message: 'GET http://internal.invalid/?token=secret',
      config: { data: 'hidden-value' },
    });
    expect(problem.code).toBe('API_UNAVAILABLE');
    expect(problem.message).toBe('无法连接 PackBreaker API，请确认后端服务已经启动');
  });
});

describe('强 ETag', () => {
  it('严格按后端 If-Match 契约编码整数版本', () => {
    expect(strongEtag(1)).toBe('"1"');
    expect(strongEtag(27)).toBe('"27"');
  });

  it('拒绝无效资源版本', () => {
    expect(() => strongEtag(0)).toThrow();
    expect(() => strongEtag(1.5)).toThrow();
  });
});
