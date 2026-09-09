import axios from 'axios';

export const AUTH_REQUIRED_EVENT = 'packbreaker:auth-required';

export interface ProblemDetails {
  type?: string;
  title?: string;
  status?: number;
  detail?: string;
  code?: string;
  trace_id?: string;
}

export class ApiProblem extends Error {
  readonly status: number | null;
  readonly code: string;
  readonly traceId: string | null;

  constructor(message: string, options?: { status?: number; code?: string; traceId?: string }) {
    super(message);
    this.name = 'ApiProblem';
    this.status = options?.status ?? null;
    this.code = options?.code ?? 'API_REQUEST_FAILED';
    this.traceId = options?.traceId ?? null;
  }
}

export const apiClient = axios.create({
  baseURL: '/api/v1',
  timeout: 15_000,
  withCredentials: true,
  xsrfCookieName: 'packbreaker_csrf',
  xsrfHeaderName: 'X-CSRF-Token',
});

function isProblemDetails(value: unknown): value is ProblemDetails {
  return value !== null && typeof value === 'object';
}

apiClient.interceptors.response.use(
  (response) => response,
  (error: unknown) => {
    if (axios.isAxiosError(error)) {
      const payload = error.response?.data;
      if (
        error.response?.status === 401 &&
        isProblemDetails(payload) &&
        payload.code === 'AUTH_REQUIRED' &&
        typeof window !== 'undefined'
      ) {
        window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
      }
    }
    return Promise.reject(error);
  },
);

export function toApiProblem(error: unknown): ApiProblem {
  if (axios.isAxiosError(error)) {
    const status = error.response?.status;
    const payload = error.response?.data;
    if (isProblemDetails(payload)) {
      const detail = typeof payload.detail === 'string' ? payload.detail : undefined;
      const title = typeof payload.title === 'string' ? payload.title : undefined;
      const code = typeof payload.code === 'string' ? payload.code : undefined;
      const traceId = typeof payload.trace_id === 'string' ? payload.trace_id : undefined;
      return new ApiProblem(
        detail ?? title ?? `PackBreaker API 请求失败（${status ?? '未知状态'}）`,
        {
          status,
          code,
          traceId,
        },
      );
    }
    if (!error.response) {
      return new ApiProblem('无法连接 PackBreaker API，请确认后端服务已经启动', {
        code: 'API_UNAVAILABLE',
      });
    }
    return new ApiProblem(`PackBreaker API 请求失败（HTTP ${status ?? '未知状态'}）`, { status });
  }
  if (error instanceof ApiProblem) return error;
  return new ApiProblem('PackBreaker API 请求失败');
}

export function strongEtag(version: number): string {
  if (!Number.isInteger(version) || version < 1) throw new Error('资源版本必须是正整数');
  return `"${version}"`;
}
