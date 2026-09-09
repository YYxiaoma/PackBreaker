import { apiClient, toApiProblem } from './client';
import type { components } from './generated/schema';

export type AuthStatus = components['schemas']['AuthStatusResponse'];
export type LoginResponse = components['schemas']['LoginResponse'];
type PasswordRequest = components['schemas']['PasswordRequest'];

export async function getAuthStatus(): Promise<AuthStatus> {
  try {
    const response = await apiClient.get<AuthStatus>('/auth/me');
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function setupAdministrator(password: string): Promise<void> {
  const payload: PasswordRequest = { password };
  try {
    await apiClient.post('/auth/setup', payload);
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function loginAdministrator(password: string): Promise<LoginResponse> {
  const payload: PasswordRequest = { password };
  try {
    const response = await apiClient.post<LoginResponse>('/auth/login', payload);
    return response.data;
  } catch (error) {
    throw toApiProblem(error);
  }
}

export async function logoutAdministrator(): Promise<void> {
  try {
    await apiClient.post('/auth/logout');
  } catch (error) {
    throw toApiProblem(error);
  }
}
