import { describe, expect, it } from 'vitest';

import { taskEventTranslation } from './taskExecutionEvents';

describe('任务执行事件中文映射', () => {
  it('已知事件显示稳定中文标题', () => {
    expect(taskEventTranslation('TASK_UNPACK_RUN_MATERIALIZED')).toMatchObject({
      title: '已创建安全拆包 Run',
      translated: true,
    });
  });

  it('未知事件明确标记为未翻译且不假装已覆盖', () => {
    expect(taskEventTranslation('TASK_NEW_UNKNOWN_EVENT')).toMatchObject({
      title: '未翻译事件',
      translated: false,
    });
  });
});
