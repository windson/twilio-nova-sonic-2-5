import { useCallback, useEffect, useRef, useState } from "react";
import {
  apiGet,
  type AppConfig,
  type CallRecord,
  type CallSummary,
  type DueRecord,
} from "@/lib/api";

// Poll cadence ported from web/app.js.
export const POLL_INTERVAL_MS = 3000;
export const CALLS_REFRESH_MS = 10000;
// After a call ends we keep polling until intelligence is terminal, bounded to
// ~50 ticks (~2.5 min at 3s) so a record that never gets intelligence does not
// poll forever. Ported verbatim from web/app.js MAX_POST_CALL_INTEL_TICKS.
export const MAX_POST_CALL_INTEL_TICKS = 50;

export function useDues() {
  const [dues, setDues] = useState<DueRecord[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    try {
      const data = await apiGet<{ dues: DueRecord[] }>("/api/dues");
      setDues(data.dues || []);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  return { dues, error, loading, reload };
}

export function useCalls() {
  const [calls, setCalls] = useState<CallSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    try {
      const data = await apiGet<{ calls: CallSummary[] }>("/api/calls");
      setCalls(data.calls || []);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
    const id = window.setInterval(() => void reload(), CALLS_REFRESH_MS);
    return () => window.clearInterval(id);
  }, [reload]);

  return { calls, error, loading, reload };
}

export function useConfig() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  useEffect(() => {
    apiGet<AppConfig>("/api/config")
      .then(setConfig)
      .catch(() => {
        /* non-fatal */
      });
  }, []);
  return config;
}

// Fetch a single call record with the live 3s polling behaviour. `live`
// controls whether polling starts immediately (e.g. right after placing a
// call). Polling continues until the call ended AND intelligence is terminal,
// bounded by MAX_POST_CALL_INTEL_TICKS; it also refreshes the calls list each
// post-call tick (via onPostCallTick).
export function useCallDetail(
  callId: string | null,
  live: boolean,
  onPostCallTick?: () => void
) {
  const [record, setRecord] = useState<CallRecord | null>(null);
  const [isLive, setIsLive] = useState(false);
  const ticksRef = useRef(0);
  const timerRef = useRef<number | null>(null);
  const cbRef = useRef(onPostCallTick);
  cbRefSync(cbRef, onPostCallTick);

  const stop = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    setIsLive(false);
  }, []);

  const fetchOnce = useCallback(async (id: string) => {
    let rec: CallRecord;
    try {
      rec = await apiGet<CallRecord>(`/api/calls/${encodeURIComponent(id)}`);
    } catch {
      // Call may not be persisted yet (still active): keep polling quietly.
      return;
    }
    setRecord(rec);

    if (rec.total_e2e_ms != null) {
      const intelTerminal =
        rec.intelligence_status === "ready" ||
        rec.intelligence_status === "failed";
      ticksRef.current += 1;
      if (intelTerminal || ticksRef.current >= MAX_POST_CALL_INTEL_TICKS) {
        stop();
      }
      cbRef.current?.();
    }
  }, [stop]);

  useEffect(() => {
    stop();
    ticksRef.current = 0;
    setRecord(null);
    if (!callId) return;
    void fetchOnce(callId);
    if (live) {
      setIsLive(true);
      timerRef.current = window.setInterval(
        () => void fetchOnce(callId),
        POLL_INTERVAL_MS
      );
    }
    return () => stop();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [callId, live]);

  return { record, isLive };
}

// Keep a mutable ref of the latest callback without re-subscribing effects.
function cbRefSync<T>(ref: React.MutableRefObject<T>, value: T) {
  ref.current = value;
}
