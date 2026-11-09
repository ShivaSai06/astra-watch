import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import { ApiError, get, getRefreshMs, subscribeRefresh } from "./api";

export const useRefreshMs = () => useSyncExternalStore(subscribeRefresh, getRefreshMs);

/** Re-reads a cached API endpoint every dashboard_refresh_seconds. These endpoints never trigger collection. */
export function usePoll<T>(path: string) {
  const ms = useRefreshMs();
  const [data, setData] = useState<T | null>(null);
  const [err, setErr] = useState("");
  const load = useCallback(() => get<T>(path).then(d => { setData(d); setErr(""); }).catch(e => setErr(e instanceof ApiError ? e.message : String(e))), [path]);
  useEffect(() => { setData(null); load(); const i = setInterval(load, ms); return () => clearInterval(i); }, [load, ms]);
  return { data, err, reload: load };
}
