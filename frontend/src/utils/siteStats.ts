export function formatSiteBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value) || value < 0) {
    return '—';
  }
  if (value < 1024) return `${Math.round(value)} B`;
  const units = ['KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB'];
  let size = value;
  let unit = -1;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(2)} ${units[unit]}`;
}

export function formatSiteDate(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '—';
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date);
}

export function formatSiteRatio(
  value: number | null | undefined,
  uploadedBytes?: number | null,
  downloadedBytes?: number | null,
): string {
  if (typeof value === 'number' && Number.isFinite(value)) return value.toFixed(2);
  if (
    uploadedBytes !== null &&
    uploadedBytes !== undefined &&
    uploadedBytes > 0 &&
    downloadedBytes === 0
  ) {
    return '∞';
  }
  return '—';
}
