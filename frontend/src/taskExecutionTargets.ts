import type { Downloader } from './api/downloaders';

/** UI listing is advisory; the server revalidates the target binding at execution. */
export function eligibleTaskExecutionTargets(downloaders: Downloader[]): Downloader[] {
  return downloaders.filter(
    (item) =>
      (item.type === 'QBITTORRENT' || item.type === 'TRANSMISSION') &&
      item.enabled &&
      item.connection_status === 'OK' &&
      item.path_mapping_status === 'OK',
  );
}

export function targetClientCheckRequired(
  downloader: Pick<Downloader, 'type'> | null,
  planRequiresCheck: boolean,
): boolean {
  return planRequiresCheck || downloader?.type === 'TRANSMISSION';
}
