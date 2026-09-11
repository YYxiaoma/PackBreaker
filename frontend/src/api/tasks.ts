import { apiClient, toApiProblem } from './client';
import type { components } from './generated/schema';

export type TaskUnit = components['schemas']['TaskUnitResponse'];
export type TaskCandidate = components['schemas']['TaskCandidateResponse'];
export type Preflight = components['schemas']['PreflightResponse'];
export type PreflightCurrent = components['schemas']['PreflightCurrentResponse'];
export type AnalyzeTaskActionInput = components['schemas']['AnalyzeTaskActionRequest'];
export type ExecuteTaskActionInput = components['schemas']['ExecuteTaskActionRequest'];
export type CancelTaskActionInput = components['schemas']['CancelTaskActionRequest'];
export type TaskMutationAction = components['schemas']['TaskMutationActionResponse'];
export type TaskRecord = components['schemas']['TaskResponse'];
export type TaskStatus = components['schemas']['TaskStatus'];
export type TaskCreateInput = components['schemas']['TaskCreateRequest'];
export type TaskCreateResult = components['schemas']['TaskCreateResponse'];
export type TaskReviewInput = components['schemas']['TaskReviewRequest'];
export type TaskReview = components['schemas']['TaskReviewResponse'];
export type TaskReviewActionInput = components['schemas']['TaskReviewActionRequest'];
export type ReviewVerification = components['schemas']['ReviewVerificationResponse'];
export type ExecutionGate = components['schemas']['ExecutionGateResponse'];
export type ExecutionPlanInput = components['schemas']['ExecutionPlanRequest'];
export type ExecutionPlan = components['schemas']['ExecutionPlanResponse'];

function taskPath(taskId: string): string {
  const normalized = taskId.trim();
  if (!normalized) throw new Error('taskId 不能为空');
  return `/tasks/${encodeURIComponent(normalized)}`;
}

function taskUnitPath(unitId: string): string {
  const normalized = unitId.trim();
  if (!normalized) throw new Error('unitId 不能为空');
  return `/task-units/${encodeURIComponent(normalized)}`;
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
  const payload: AnalyzeTaskActionInput = { action: 'analyze', source_root: sourceRoot };
  try {
    const response = await apiClient.post<Preflight>(`${taskPath(taskId)}/actions`, payload);
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

function actionHeaders(idempotencyKey: string): { 'Idempotency-Key': string } {
  const normalized = idempotencyKey.trim();
  if (!normalized) throw new Error('Idempotency-Key 不能为空');
  return { 'Idempotency-Key': normalized };
}

export async function executeTask(
  taskId: string,
  executionPlanId: string,
  idempotencyKey: string,
): Promise<TaskMutationAction> {
  const payload: ExecuteTaskActionInput = {
    action: 'execute',
    execution_plan_id: executionPlanId,
  };
  try {
    const response = await apiClient.post<TaskMutationAction>(
      `${taskPath(taskId)}/actions`,
      payload,
      {
        headers: actionHeaders(idempotencyKey),
      },
    );
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function cancelTask(
  taskId: string,
  options: Pick<CancelTaskActionInput, 'remove_downloader_task' | 'rollback_created_resources'>,
  idempotencyKey: string,
): Promise<TaskMutationAction> {
  const payload: CancelTaskActionInput = { action: 'cancel', ...options };
  try {
    const response = await apiClient.post<TaskMutationAction>(
      `${taskPath(taskId)}/actions`,
      payload,
      {
        headers: actionHeaders(idempotencyKey),
      },
    );
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function getTaskUnitDecision(unitId: string): Promise<TaskReview> {
  try {
    const response = await apiClient.get<TaskReview>(`${taskUnitPath(unitId)}/decision`);
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function submitTaskUnitDecision(
  unitId: string,
  payload: TaskReviewInput,
): Promise<TaskReview> {
  try {
    const response = await apiClient.post<TaskReview>(`${taskUnitPath(unitId)}/decision`, payload);
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function getTaskUnitReviewVerification(unitId: string): Promise<ReviewVerification> {
  try {
    const response = await apiClient.get<ReviewVerification>(
      `${taskUnitPath(unitId)}/decision/verification`,
    );
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function reverifyTaskUnitDecision(unitId: string): Promise<ReviewVerification> {
  const payload: TaskReviewActionInput = { action: 'reverify' };
  try {
    const response = await apiClient.post<ReviewVerification>(
      `${taskUnitPath(unitId)}/decision/actions`,
      payload,
    );
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function getTaskUnitExecutionGate(unitId: string): Promise<ExecutionGate> {
  try {
    const response = await apiClient.get<ExecutionGate>(`${taskUnitPath(unitId)}/execution-gate`);
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function refreshTaskUnitExecutionGate(unitId: string): Promise<ExecutionGate> {
  try {
    const response = await apiClient.post<ExecutionGate>(`${taskUnitPath(unitId)}/execution-gate`);
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function getTaskUnitExecutionPlan(unitId: string): Promise<ExecutionPlan> {
  try {
    const response = await apiClient.get<ExecutionPlan>(`${taskUnitPath(unitId)}/execution-plan`);
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function createTaskUnitExecutionPlan(
  unitId: string,
  targetRoot: string,
  targetDownloaderId: string,
): Promise<ExecutionPlan> {
  const payload: ExecutionPlanInput = {
    target_root: targetRoot,
    target_downloader_id: targetDownloaderId,
  };
  try {
    const response = await apiClient.post<ExecutionPlan>(
      `${taskUnitPath(unitId)}/execution-plan`,
      payload,
    );
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}
