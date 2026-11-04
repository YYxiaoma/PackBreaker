export interface CookieCloudConnectionDraft {
  serverUrl: string;
  uuid: string;
  password: string;
  clearPassword: boolean;
  requestTimeoutSeconds: number;
}

export interface SavedCookieCloudConnection {
  server_url: string;
  uuid: string;
  request_timeout_seconds: number;
}

export function hasCookieCloudConnectionDraftChanged(
  draft: CookieCloudConnectionDraft,
  saved: SavedCookieCloudConnection | undefined,
): boolean {
  if (!saved) return false;
  return (
    draft.serverUrl.trim() !== saved.server_url ||
    draft.uuid.trim() !== saved.uuid ||
    draft.password.length > 0 ||
    draft.clearPassword ||
    draft.requestTimeoutSeconds !== saved.request_timeout_seconds
  );
}
