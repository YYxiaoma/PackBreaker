export const BACKGROUND_RELEASE_CHECK_INTERVAL_MS = 30 * 60 * 1000;

export function shouldMarkUpdateUnseen(updateAvailable: boolean, popoverVisible: boolean): boolean {
  return updateAvailable && !popoverVisible;
}
