/**
 * Client for /api/v2/assistant/query and /api/v1/assistant/query/stream --
 * the voice-first runtime's conversation paths. Consumes presentation-aware
 * contracts (display_text for rich markdown UI, speech_text for conversational TTS).
 */
import { consumeSSE, StreamEvent } from './sse';
import { AssistantResponseQuality, TARSAssistantMessage } from '../types/assistant-message';

export interface AssistantStreamCompletePayload {
  message?: TARSAssistantMessage | Record<string, unknown>;
  display_text?: string;
  speech_text?: string;
  quality?: AssistantResponseQuality;
  [key: string]: unknown;
}

export interface AssistantStreamCallbacks {
  onDelta?: (text: string) => void;
  onComplete?: (payload: AssistantStreamCompletePayload | undefined) => void;
  onError?: (detail: string) => void;
}

export interface AssistantQueryResult {
  message: TARSAssistantMessage;
  display_text: string;
  speech_text: string;
  quality?: AssistantResponseQuality;
}

export class AssistantClient {
  /**
   * Non-streaming assistant query targeting /api/v2/assistant/query
   * with fallback to legacy /api/v1/assistant/query.
   */
  public async query(
    text: string,
    conversationId: string | undefined,
    apiEndpoint: string,
    telemetryIdOrSignal?: string | AbortSignal,
    signal?: AbortSignal
  ): Promise<AssistantQueryResult> {
    let telemetryId: string | undefined;
    let actualSignal = signal;
    if (telemetryIdOrSignal instanceof AbortSignal) {
      actualSignal = telemetryIdOrSignal;
    } else if (typeof telemetryIdOrSignal === 'string') {
      telemetryId = telemetryIdOrSignal;
    }

    const endpoint = `${apiEndpoint.replace(/\/$/, '')}/api/v2/assistant/query`;
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (telemetryId) {
      headers['X-TARS-Voice-Turn-ID'] = telemetryId;
    }

    let res = await fetch(endpoint, {
      method: 'POST',
      headers,
      body: JSON.stringify({ text, conversation_id: conversationId }),
      signal: actualSignal,
    });

    if (res.status === 404) {
      // Fallback to v1 endpoint
      res = await fetch(`${apiEndpoint.replace(/\/$/, '')}/api/v1/assistant/query`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ text, conversation_id: conversationId }),
        signal: actualSignal,
      });
    }

    if (!res.ok) {
      const errorText = await res.text().catch(() => '');
      throw new Error(`Assistant query failed (${res.status}): ${errorText || res.statusText}`);
    }

    const data = await res.json();
    const message: TARSAssistantMessage = data.message || data;
    const display_text = data.display_text || message.content || data.content || '';
    const speech_text = data.speech_text || message.speech_text || display_text;

    return {
      message,
      display_text,
      speech_text,
      quality: data.quality,
    };
  }

  /**
   * Streaming twin of query -- consumes Server-Sent Events from
   * /api/v1/assistant/query/stream.
   */
  public async streamQuery(
    text: string,
    conversationId: string | undefined,
    apiEndpoint: string,
    callbacks: AssistantStreamCallbacks,
    telemetryIdOrSignal?: string | AbortSignal,
    signal?: AbortSignal
  ): Promise<void> {
    let telemetryId: string | undefined;
    let actualSignal = signal;
    if (telemetryIdOrSignal instanceof AbortSignal) {
      actualSignal = telemetryIdOrSignal;
    } else if (typeof telemetryIdOrSignal === 'string') {
      telemetryId = telemetryIdOrSignal;
    }

    let res: Response;
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (telemetryId) {
      headers['X-TARS-Voice-Turn-ID'] = telemetryId;
    }

    try {
      res = await fetch(`${apiEndpoint.replace(/\/$/, '')}/api/v1/assistant/query/stream`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ text, conversation_id: conversationId }),
        signal: actualSignal,
      });
    } catch (err) {
      if (actualSignal?.aborted) return;
      callbacks.onError?.(err instanceof Error ? err.message : String(err));
      return;
    }

    if (!res.ok || !res.body) {
      callbacks.onError?.(`Assistant stream request failed with status ${res.status}`);
      return;
    }

    try {
      await consumeSSE(res.body, (event: StreamEvent) => {
        if (event.type === 'delta' && event.text) {
          callbacks.onDelta?.(event.text);
        } else if (event.type === 'complete') {
          callbacks.onComplete?.({
            message: event.message as TARSAssistantMessage | Record<string, unknown> | undefined,
            display_text: event.display_text as string | undefined,
            speech_text: event.speech_text as string | undefined,
            quality: event.quality as AssistantResponseQuality | undefined,
          });
        } else if (event.type === 'error') {
          callbacks.onError?.(event.detail || 'Assistant stream reported an error');
        }
      });
    } catch (err) {
      if (actualSignal?.aborted) return;
      callbacks.onError?.(err instanceof Error ? err.message : String(err));
    }
  }
}

export const assistantClient = new AssistantClient();
