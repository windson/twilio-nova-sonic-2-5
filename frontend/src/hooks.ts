import { useCallback, useEffect, useRef, useState } from "react";
import { isIntelligenceTerminal } from "@/lib/selectors";
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
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const activeIdRef = useRef<string | null>(null);
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
    } catch (e) {
      if (activeIdRef.current !== id) return;
      setError((e as Error).message || "The call could not be loaded.");
      setLoading(false);
      // An active call may not be persisted yet; its polling continues.
      return;
    }
    // A slower response for the previously selected call must not replace
    // the operator's current selection.
    if (activeIdRef.current !== id) return;
    setRecord(rec);
    setLoading(false);
    setError(null);

    if (rec.total_e2e_ms != null) {
      const intelTerminal = isIntelligenceTerminal(rec.intelligence_status);
      ticksRef.current += 1;
      if (intelTerminal || ticksRef.current >= MAX_POST_CALL_INTEL_TICKS) {
        stop();
      }
      cbRef.current?.();
    }
  }, [stop]);

  useEffect(() => {
    stop();
    activeIdRef.current = callId;
    ticksRef.current = 0;
    setRecord(null);
    setError(null);
    setLoading(Boolean(callId));
    if (!callId) return;
    void fetchOnce(callId);
    if (live) {
      setIsLive(true);
      timerRef.current = window.setInterval(
        () => void fetchOnce(callId),
        POLL_INTERVAL_MS
      );
    }
    return () => {
      activeIdRef.current = null;
      stop();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [callId, live]);

  const reload = useCallback(() => {
    if (!callId) return;
    setLoading(true);
    void fetchOnce(callId);
  }, [callId, fetchOnce]);

  return { record, isLive, loading, error, reload };
}

// Keep a mutable ref of the latest callback without re-subscribing effects.
function cbRefSync<T>(ref: React.MutableRefObject<T>, value: T) {
  ref.current = value;
}
