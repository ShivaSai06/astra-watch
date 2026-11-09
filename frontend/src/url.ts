// Remove marketing/tracking parameters (utm_*, gclid, fbclid, ...) that other sites append to a link.
// Astra Watch itself never adds them and uses no query parameters: routing is hash-based (#/machines)
// and sign-in is handled by Tailscale, so nothing needed is lost. Any other parameter is left alone.
const TRACKING = /^(utm_[a-z0-9_]+|gclid|dclid|fbclid|msclkid|mc_eid|mc_cid|igshid|_hsenc|_hsmi)$/i;

/** Returns the URL without tracking parameters, or null when there was nothing to remove. */
export function cleanedUrl(loc: Pick<Location, "pathname" | "search" | "hash">): string | null {
  const params = new URLSearchParams(loc.search);
  const drop = [...params.keys()].filter(k => TRACKING.test(k));
  if (!drop.length) return null;
  drop.forEach(k => params.delete(k));
  const q = params.toString();
  return loc.pathname + (q ? `?${q}` : "") + loc.hash;
}

/** Rewrites the address bar in place (no reload, no new history entry, hash route preserved). */
export function stripTrackingParams(): void {
  try {
    const next = cleanedUrl(window.location);
    if (next) window.history.replaceState(window.history.state, "", next);
  } catch { /* cosmetic only */ }
}
