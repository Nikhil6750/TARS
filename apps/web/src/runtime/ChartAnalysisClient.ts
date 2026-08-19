/**
 * "Analyze this chart" client for the voice-first runtime. Captures the
 * current/previous foreground window through the real Win32 capture
 * (native-bridge.ts), then streams the analysis so a status line and
 * commentary can show immediately instead of a frozen wait -- see
 * assistant/chart_analysis.py's analyze_stream for the matching backend
 * side (status/delta/complete events + stage timings).
 */
import { nativeBridge } from '../services/native-bridge';
import { ChartAnalysisData } from '../components/hud/ChartAnalysisCard';
import { consumeSSE, StreamEvent } from './sse';

export interface ChartTiming {
  capture_ms: number;
  claude_start_ms: number | null;
  first_token_ms: number | null;
  complete_ms: number | null;
}

export interface ChartAnalysisCallbacks {
  onStatus?: (text: string) => void;
  onDelta?: (text: string) => void;
  onComplete?: (result: ChartAnalysisData, timing: ChartTiming) => void;
  onError?: (detail: string) => void;
}

interface ChartStreamEvent extends StreamEvent {
  result?: ChartAnalysisData;
  timing?: Partial<ChartTiming>;
}

export class ChartAnalysisClient {
  public async analyze(
    apiEndpoint: string,
    conversationId: string,
    callbacks: ChartAnalysisCallbacks,
    signal?: AbortSignal
  ): Promise<void> {
    // Perf instrumentation (TARS MASTER MILESTONE Phase 2). All timestamps
    // relative to t0 = command received. Combine with the backend's own
    // timing (returned in the complete event) and the native
    // [PERF][capture_chart_window] stderr line to see every stage of a
    // real "Analyze this chart" request.
    const t0 = performance.now();
    const mark = (label: string) => console.info(`[PERF][chart] ${label} at ${Math.round(performance.now() - t0)}ms`);
    mark('command_received');

    callbacks.onStatus?.('Looking at the chart...');
    mark('intent_detected');
    const captureStarted = performance.now();

    const [activeContext, capture] = await Promise.all([
      nativeBridge.getActiveWindowContext(),
      nativeBridge.captureChartWindow(true),
    ]);
    const captureMs = Math.round(performance.now() - captureStarted);
    mark('hide_capture_restore_complete');
    if (signal?.aborted) return;

    if (capture.error || capture.is_secure_desktop) {
      callbacks.onError?.(capture.error || 'Capture was refused (secure desktop) -- nothing to analyze.');
      return;
    }

    mark('http_request_start');
    let res: Response;
    try {
      res = await fetch(`${apiEndpoint}/api/v1/assistant/analyze-chart/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          conversation_id: conversationId,
          capture,
          active_context: activeContext,
        }),
        signal,
      });
    } catch (err) {
      if (signal?.aborted) return;
      callbacks.onError?.(err instanceof Error ? err.message : String(err));
      return;
    }
    mark('http_response_headers_received');

    if (!res.ok || !res.body) {
      callbacks.onError?.(`Chart analysis request failed with status ${res.status}`);
      return;
    }

    let timing: ChartTiming = {
      capture_ms: captureMs,
      claude_start_ms: null,
      first_token_ms: null,
      complete_ms: null,
    };

    let gotFirstDelta = false;
    try {
      await consumeSSE(res.body, (event: ChartStreamEvent) => {
        if (event.type === 'status' && event.text) {
          callbacks.onStatus?.(event.text);
        } else if (event.type === 'delta' && event.text) {
          if (!gotFirstDelta) {
            gotFirstDelta = true;
            mark('frontend_first_delta_received');
          }
          callbacks.onDelta?.(event.text);
        } else if (event.type === 'complete' && event.result) {
          timing = { ...timing, ...(event.timing || {}) };
          mark(`frontend_complete (backend timing=${JSON.stringify(event.timing)})`);
          callbacks.onComplete?.(event.result, timing);
        } else if (event.type === 'error') {
          callbacks.onError?.(event.detail || 'Chart analysis failed');
        }
      });
    } catch (err) {
      if (signal?.aborted) return;
      callbacks.onError?.(err instanceof Error ? err.message : String(err));
    }
  }
}

export const chartAnalysisClient = new ChartAnalysisClient();
