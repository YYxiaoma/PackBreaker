import { describe, expect, it } from 'vitest';

import {
  unpackDefinitionStatusLabel,
  unpackExecutionStatusLabel,
  unpackItemStatusLabel,
  unpackProgressPercent,
  unpackSourceKindLabel,
  unpackTriggerLabel,
} from './unpackPresentation';

describe('unpack presentation', () => {
  it('translates every user-visible definition status', () => {
    expect(unpackDefinitionStatusLabel('PENDING_EXECUTION')).toBe('待执行');
    expect(unpackDefinitionStatusLabel('ENABLED')).toBe('已启用');
    expect(unpackDefinitionStatusLabel('PAUSED')).toBe('已暂停');
    expect(unpackDefinitionStatusLabel('ERROR')).toBe('异常');
  });

  it('translates execution and item states without exposing enum values', () => {
    expect(unpackExecutionStatusLabel('REVIEW_REQUIRED')).toBe('待人工审核');
    expect(unpackExecutionStatusLabel('CLIENT_VERIFYING')).toBe('下载器校验中');
    expect(unpackItemStatusLabel('MATCHED_AUTO')).toBe('自动匹配成功');
    expect(unpackItemStatusLabel('AUXILIARY_FETCHING')).toBe('补齐辅助文件');
    expect(unpackItemStatusLabel('EXECUTION_ERROR')).toBe('执行错误');
  });

  it('translates task kind/source and clamps progress', () => {
    expect(unpackTriggerLabel('MANUAL')).toBe('手动拆包');
    expect(unpackTriggerLabel('MONITOR')).toBe('监控拆包');
    expect(unpackSourceKindLabel('DIRECTORY')).toBe('目录');
    expect(unpackSourceKindLabel('DOWNLOADER')).toBe('下载器');
    expect(unpackProgressPercent(10, 3)).toBe(30);
    expect(unpackProgressPercent(0, 0)).toBe(0);
  });
});
