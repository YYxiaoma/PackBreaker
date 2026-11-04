import { describe, expect, it } from 'vitest';

import { siteFaviconUrl, siteIconUrl } from './siteIcon';

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
});
