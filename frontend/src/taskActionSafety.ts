import type { TaskStatus } from './api/tasks';

export type TaskMutationKind = 'execute' | 'cancel' | 'reconcile';

const CANCELLABLE_STATUSES = new Set<TaskStatus>([
  'LINKING',
  'ADDING',
  'CLIENT_VERIFYING',
  'SEEDING',
]);

const CANCELLATION_PROGRESS_STATUSES = new Set<TaskStatus>(['CANCELLING', 'ROLLING_BACK']);

export function canStartTaskCancellation(status: TaskStatus): boolean {
  return CANCELLABLE_STATUSES.has(status);
}

export function cancellationIsInProgress(status: TaskStatus): boolean {
  return CANCELLATION_PROGRESS_STATUSES.has(status);
}

export function cancellationOptionsAreConsistent(
  removeDownloaderTask: boolean,
  rollbackCreatedResources: boolean,
): boolean {
  return !rollbackCreatedResources || removeDownloaderTask;
}

function defaultActionNonce(): string {
  return (
    globalThis.crypto?.randomUUID?.() ??
    `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
  );
}

export function createTaskActionIdempotencyKey(
  action: TaskMutationKind,
  taskId: string,
  uuidFactory: () => string = defaultActionNonce,
): string {
  const normalizedTaskId = taskId.trim();
  if (!normalizedTaskId) throw new Error('taskId 不能为空');
  const uuid = uuidFactory().trim();
  if (!uuid || /\s/.test(uuid)) throw new Error('无法生成安全的任务动作幂等键');
  return `ui-${action}-${normalizedTaskId}-${uuid}`;
}

export function formatByteUpperBound(value: number): string {
  if (!Number.isFinite(value) || value < 0) return '未知';
  if (value < 1024) return `${Math.trunc(value)} B`;
  const units = ['KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  let scaled = value;
  let index = -1;
  do {
    scaled /= 1024;
    index += 1;
  } while (scaled >= 1024 && index < units.length - 1);
  return `${scaled.toFixed(scaled >= 100 ? 0 : scaled >= 10 ? 1 : 2)} ${units[index]}`;
}
