// Pull cookies Chrome would send for one or more URLs, dedup across them,
// and return a flat array in a shape the desktop app can replay verbatim.
//
// Why pull for multiple URLs? Streaming sites often host the player on one
// domain (egydead.example) and the video CDN on another (vibuxer-cdn.example).
// Session cookies may be set on the parent site's eTLD+1 AND the CDN. The
// desktop app doesn't know which; it just gets everything and lets the
// aiohttp ClientSession match by domain at request time.

/**
 * @param {string[]} urls - list of URLs whose cookies we want Chrome to send.
 * @returns {Promise<Array<{name:string,value:string,domain:string,path:string,secure:boolean,httpOnly:boolean,sameSite:string}>>}
 */
export async function captureCookiesFor(urls) {
  const bag = new Map();
  for (const url of urls) {
    if (!url || !/^https?:\/\//i.test(url)) continue;
    let cookies = [];
    try {
      cookies = await chrome.cookies.getAll({ url });
    } catch (e) {
      console.warn('[CastBooster] cookies.getAll failed for', url, e);
      continue;
    }
    for (const c of cookies) {
      const key = `${c.name}\u0000${c.domain}\u0000${c.path}`;
      if (!bag.has(key)) bag.set(key, c);
    }
  }
  return Array.from(bag.values()).map((c) => ({
    name: c.name,
    value: c.value,
    domain: c.domain,
    path: c.path,
    secure: !!c.secure,
    httpOnly: !!c.httpOnly,
    sameSite: c.sameSite || 'unspecified',
  }));
}
