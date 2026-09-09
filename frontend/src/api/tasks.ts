import { apiClient, toApiProblem } from './client';
import type { components } from './generated/schema';

export type TaskUnit = components['schemas']['TaskUnitResponse'];
export type TaskCandidate = components['schemas']['TaskCandidateResponse'];
export type Preflight = components['schemas']['PreflightResponse'];
export type PreflightCurrent = components['schemas']['PreflightCurrentResponse'];
export type TaskActionInput = components['schemas']['TaskActionRequest'];
export type TaskRecord = components['schemas']['TaskResponse'];
export type TaskStatus = components['schemas']['TaskStatus'];
export type TaskCreateInput = components['schemas']['TaskCreateRequest'];
export type TaskCreateResult = components['schemas']['TaskCreateResponse'];

function taskPath(taskId: string): string {
  const normalized = taskId.trim();
  if (!normalized) throw new Error('taskId 不能为空');
  return `/tasks/${encodeURIComponent(normalized)}`;
}

export async function listTasks(status?: TaskStatus): Promise<TaskRecord[]> {
  try {
    const response = await apiClient.get<{ items: TaskRecord[] }>('/tasks', {
      params: status ? { status } : undefined,
    });
    return response.data.items;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function getTask(taskId: string): Promise<TaskRecord> {
  try {
    const response = await apiClient.get<TaskRecord>(taskPath(taskId));
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function createTask(payload: TaskCreateInput): Promise<TaskCreateResult> {
  try {
    const response = await apiClient.post<TaskCreateResult>('/tasks', payload);
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function listTaskUnits(taskId: string): Promise<TaskUnit[]> {
  try {
    const response = await apiClient.get<{ items: TaskUnit[] }>(`${taskPath(taskId)}/units`);
    return response.data.items;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function listTaskCandidates(taskId: string): Promise<TaskCandidate[]> {
  try {
    const response = await apiClient.get<{ items: TaskCandidate[] }>(
      `${taskPath(taskId)}/candidates`,
    );
    return response.data.items;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function getTaskPreflight(taskId: string): Promise<Preflight> {
  try {
    const response = await apiClient.get<Preflight>(`${taskPath(taskId)}/preflight`);
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function getTaskPreflightCurrent(taskId: string): Promise<PreflightCurrent> {
  try {
    const response = await apiClient.get<PreflightCurrent>(`${taskPath(taskId)}/preflight/current`);
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function analyzeTask(taskId: string, sourceRoot: string): Promise<Preflight> {
  const payload: TaskActionInput = { action: 'analyze', source_root: sourceRoot };
  try {
    const response = await apiClient.post<Preflight>(`${taskPath(taskId)}/actions`, payload);
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}
