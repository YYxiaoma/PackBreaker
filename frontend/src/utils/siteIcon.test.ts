import { describe, expect, it } from 'vitest';

import { SITE_ICON_PATHS, siteFaviconUrl, siteIconUrl } from './siteIcon';

const bundledIcons = new Set(
  Object.keys(import.meta.glob('/public/icons/site/*')).map((path) => path.split('/').pop() ?? ''),
);

describe('site icon URL resolution', () => {
  it('prefers a bundled site-specific icon when available', () => {
    expect(siteIconUrl('MTEAM', 'https://kp.m-team.cc')).toBe('/icons/site/mteam.ico');
    expect(siteIconUrl('HDFANS', 'https://hdfans.org')).toBe('/icons/site/hdf.ico');
    expect(siteIconUrl('SPRING_SUNDAY', 'https://springsunday.net')).toBe(
      '/icons/site/springsunday.png',
    );
    expect(siteIconUrl('U2', 'https://u2.dmhy.org')).toBe('/icons/site/u2.ico');
  });

  it('builds the configured site origin favicon URL', () => {
    expect(siteFaviconUrl('https://tracker.example.test/path')).toBe(
      'https://tracker.example.test/favicon.ico',
    );
  });

  it('returns an empty URL for invalid fallback origins', () => {
    expect(siteFaviconUrl('not a URL')).toBe('');
    expect(siteIconUrl('U2', 'not a URL')).toBe('/icons/site/u2.ico');
  });

  it('ships a real icon file for every mapped site kind', () => {
    expect(bundledIcons.size).toBeGreaterThan(0);
    const missing = Object.entries(SITE_ICON_PATHS).filter(([, file]) => !bundledIcons.has(file));
    expect(missing).toEqual([]);
  });

  it('uses the icons synced from PT-depiler for candidate sites', () => {
    expect(siteIconUrl('OURBITS', 'https://ourbits.club')).toBe('/icons/site/ourbits.ico');
    expect(siteIconUrl('HDCITY', 'https://hdcity.city')).toBe('/icons/site/hdcity.png');
    expect(siteIconUrl('CSPT', 'https://cspt.top')).toBe('/icons/site/cspt.ico');
    expect(siteIconUrl('HAIDAN', 'https://www.haidan.cc')).toBe('/icons/site/haidan.png');
  });

  it('falls back to the site favicon when upstream ships no icon asset', () => {
    expect(siteIconUrl('LEMONHD', 'https://lemonhd.club')).toBe('https://lemonhd.club/favicon.ico');
    expect(siteIconUrl('PTCHINA', 'https://ptchina.org')).toBe('https://ptchina.org/favicon.ico');
  });
});
