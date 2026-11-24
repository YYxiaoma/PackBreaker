/**
 * 手动同步的「普通同步 / 强制重试」判定。
 *
 * 后端 `POST /sites/{id}/sync?force_retry=` 在 force_retry 为真时会先重置熔断，
 * 并在熔断或连接异常时先跑一次连接测试。对正常站点这样做会平白多打一次站点，
 * 所以只在站点确实处于异常态时才启用。
 */

type SyncStateSource = {
  site: { id: string; connection_status: string };
  connection: { circuit_state: string };
};

export function siteSyncNeedsReset(
  source: SyncStateSource | null | undefined,
  siteId?: string | null,
): boolean {
  if (!source) return false;
  if (siteId != null && source.site.id !== siteId) return false;
  return source.connection.circuit_state !== 'CLOSED' || source.site.connection_status !== 'OK';
}

/** 异常站点的点击语义是「重试」，正常站点才是「同步」。 */
export function siteSyncButtonLabel(
  needsReset: boolean,
  circuitState: string | null | undefined,
): string {
  if (!needsReset) return '立即同步';
  return circuitState === 'OPEN' ? '重试并重置熔断' : '重新验证并同步';
}
