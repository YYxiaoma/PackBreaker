import { describe, expect, it } from 'vitest';

import { createClientNonce } from './clientNonce';

describe('客户端 nonce 生成', () => {
  it('优先使用浏览器原生 randomUUID', () => {
    const source = {
      randomUUID: () => '11111111-2222-4333-8444-555555555555',
      getRandomValues: () => {
        throw new Error('不应进入 getRandomValues fallback');
      },
    } as unknown as Crypto;

    expect(createClientNonce(source)).toBe('11111111-2222-4333-8444-555555555555');
  });

  it('HTTP 内网环境没有 randomUUID 时使用 getRandomValues 生成 RFC4122 v4', () => {
    const source = {
      getRandomValues: (bytes: Uint8Array) => {
        bytes.fill(0);
        return bytes;
      },
    } as unknown as Crypto;

    expect(createClientNonce(source)).toBe('00000000-0000-4000-8000-000000000000');
  });

  it('Web Crypto 完全不可用时仍生成无空白的幂等 nonce', () => {
    const value = createClientNonce(undefined);
    expect(value).toBeTruthy();
    expect(value).not.toMatch(/\s/);
  });
});
