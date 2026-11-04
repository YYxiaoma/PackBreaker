export type CookieCloudProfileSyncWaitResult = 'completed' | 'stopped' | 'unavailable';

interface ProfileSyncStatus {
  pending_count: number;
}

interface WaitOptions {
  intervalMs?: number;
  sleep?: (milliseconds: number) => Promise<void>;
}

const defaultSleep = (milliseconds: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, milliseconds));

/** Wait until the background profile queue drains, without imposing a shorter UI deadline than site requests. */
export async function waitForCookieCloudProfileSync(
  getStatus: () => Promise<ProfileSyncStatus>,
  shouldContinue: () => boolean,
  options: WaitOptions = {},
): Promise<CookieCloudProfileSyncWaitResult> {
  const intervalMs = options.intervalMs ?? 5_000;
  const sleep = options.sleep ?? defaultSleep;

  while (shouldContinue()) {
    let status: ProfileSyncStatus;
    try {
      status = await getStatus();
    } catch {
      return 'unavailable';
    }
    if (!shouldContinue()) return 'stopped';
    if (status.pending_count === 0) return 'completed';
    await sleep(intervalMs);
  }

  return 'stopped';
}
