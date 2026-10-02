export const BACKGROUND_RELEASE_CHECK_INTERVAL_MS = 30 * 60 * 1000;
export const PENDING_UPGRADE_SESSION_KEY = 'pb-pending-upgrade';
export const LAST_RELOADED_UPGRADE_REQUEST_KEY = 'pb-last-reloaded-upgrade-request';

export interface PendingUpgradeRequest {
  idempotencyKey: string;
  targetVersion: string;
  targetDigest: string;
}

export function shouldMarkUpdateUnseen(updateAvailable: boolean, popoverVisible: boolean): boolean {
  return updateAvailable && !popoverVisible;
}

export function parsePendingUpgradeRequest(raw: string | null): PendingUpgradeRequest | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<PendingUpgradeRequest>;
    if (
      typeof parsed.idempotencyKey !== 'string' ||
      !parsed.idempotencyKey ||
      typeof parsed.targetVersion !== 'string' ||
      !parsed.targetVersion ||
      typeof parsed.targetDigest !== 'string' ||
      !/^sha256:[0-9a-f]{64}$/.test(parsed.targetDigest)
    ) {
      return null;
    }
    return {
      idempotencyKey: parsed.idempotencyKey,
      targetVersion: parsed.targetVersion,
      targetDigest: parsed.targetDigest,
    };
  } catch {
    return null;
  }
}

export function shouldReloadAfterUpgrade(
  phase: string | undefined,
  requestId: string | undefined,
  currentVersion: string,
  targetVersion: string | undefined,
  lastReloadedRequestId: string | null,
): boolean {
  return Boolean(
    phase === 'succeeded' &&
      requestId &&
      targetVersion &&
      currentVersion === targetVersion &&
      requestId !== lastReloadedRequestId,
  );
}
