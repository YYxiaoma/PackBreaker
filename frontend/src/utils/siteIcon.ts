import type { SiteKind } from '../api/sites';

const SITE_ICON_PATHS: Partial<Record<SiteKind, string>> = {
  MTEAM: 'mteam.ico',
  HDTIME: 'hdtime.ico',
  HHCLUB: 'hhanclub.png',
  KEEPFRDS: 'keepfrds.png',
  HDHOME: 'hdhome.ico',
  UBITS: 'ubits.ico',
  U2: 'u2.ico',
  HDFANS: 'hdf.ico',
  BTSCHOOL: 'btschool.ico',
  PTTIME: 'pttime.ico',
  ROUSI_PRO: 'rousipro.ico',
  LINGYIN_CLUB: 'soulvoice.ico',
  PTERCLUB: 'pter.ico',
  AUDIENCES: 'audiences.ico',
  SPRING_SUNDAY: 'springsunday.png',
  HDDOLBY: 'hddolby.ico',
  TANGPT: 'tangpt.png',
  CARPT: 'carpt.ico',
};

export function siteFaviconUrl(baseUrl: string): string {
  try {
    return new URL('/favicon.ico', baseUrl).toString();
  } catch {
    return '';
  }
}

/**
 * Prefer a bundled, site-specific icon (following PT-Depiler's local-icon
 * lookup pattern); use the site's own favicon for sites without a bundled icon.
 */
export function siteIconUrl(kind: SiteKind, baseUrl: string): string {
  const iconPath = SITE_ICON_PATHS[kind];
  return iconPath ? `/icons/site/${iconPath}` : siteFaviconUrl(baseUrl);
}
