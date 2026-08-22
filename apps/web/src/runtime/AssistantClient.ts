/** Client for the one canonical backend-owned assistant turn API. */
import { TARSAssistantMessage } from '../types/assistant-message';
import { consumeSSE, StreamEvent } from './sse';

export interface AssistantResponsePayload {
  turn_id: string;
  display_text: string;
  speech_text: string;
  intent: string;
  status: string;
  provider: string;
  latency_ms: number;
  conversation_id: string;
  message?: TARSAssistantMessage;
}

export interface AssistantStreamCallbacks {
  onDelta?: (text: string) => void;
  onComplete?: (payload: AssistantResponsePayload) => void;
  onError?: (detail: string) => void;
}

export interface AssistantQueryResult extends AssistantResponsePayload {
  message: TARSAssistantMessage;
}

function projectMessage(payload: AssistantResponsePayload): TARSAssistantMessage {
  return {
    schema_version: '1.0.0',
    message_id: payload.turn_id,
    conversation_id: payload.conversation_id,
    timestamp: new Date().toISOString(),
    role: 'assistant',
    content: payload.display_text,
    input_mode: 'text',
    intent: payload.intent,
    providers: { assistant: payload.provider },
    error: payload.status === 'failed' ? payload.display_text : null,
    display_text: payload.display_text,
    speech_text: payload.speech_text,
  };
}

function parseCompleteEvent(event: StreamEvent): AssistantResponsePayload {
  const payload: AssistantResponsePayload = {
    turn_id: String(event.turn_id ?? ''),
    display_text: String(event.display_text ?? ''),
    speech_text: String(event.speech_text ?? ''),
    intent: String(event.intent ?? 'NORMAL_CONVERSATION'),
    status: String(event.status ?? 'completed'),
    provider: String(event.provider ?? 'unknown'),
    latency_ms: Number(event.latency_ms ?? 0),
    conversation_id: String(event.conversation_id ?? crypto.randomUUID()),
  };
  payload.message = projectMessage(payload);
  return payload;
}

export class AssistantClient {
  public async query(
    text: string,
    conversationId: string | undefined,
    apiEndpoint: string,
    turnIdOrSignal?: string | AbortSignal,
    signal?: AbortSignal
  ): Promise<AssistantQueryResult> {
    const turnId = typeof turnIdOrSignal === 'string' ? turnIdOrSignal : crypto.randomUUID();
    const actualSignal = turnIdOrSignal instanceof AbortSignal ? turnIdOrSignal : signal;
    const response = await fetch(`${apiEndpoint.replace(/\/$/, '')}/api/v1/assistant/query`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-TARS-Turn-ID': turnId,
      },
      body: JSON.stringify({ text, conversation_id: conversationId, turn_id: turnId }),
      signal: actualSignal,
    });
    if (!response.ok) {
      const detail = await response.text().catch(() => response.statusText);
      throw new Error(`Assistant query failed (${response.status}): ${detail}`);
    }
    const payload = (await response.json()) as AssistantResponsePayload;
    return { ...payload, message: projectMessage(payload) };
  }

  public async streamQuery(
    text: string,
    conversationId: string | undefined,
    apiEndpoint: string,
    callbacks: AssistantStreamCallbacks,
    turnIdOrSignal?: string | AbortSignal,
    signal?: AbortSignal
  ): Promise<void> {
    const turnId = typeof turnIdOrSignal === 'string' ? turnIdOrSignal : crypto.randomUUID();
    const actualSignal = turnIdOrSignal instanceof AbortSignal ? turnIdOrSignal : signal;
    let response: Response;
    try {
      response = await fetch(
        `${apiEndpoint.replace(/\/$/, '')}/api/v1/assistant/query/stream`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-TARS-Turn-ID': turnId,
          },
          body: JSON.stringify({ text, conversation_id: conversationId, turn_id: turnId }),
          signal: actualSignal,
        }
      );
    } catch (error) {
      if (!actualSignal?.aborted) {
        callbacks.onError?.(error instanceof Error ? error.message : String(error));
      }
      return;
    }
    if (!response.ok || !response.body) {
      callbacks.onError?.(`Assistant stream request failed with status ${response.status}`);
      return;
    }

    try {
      await consumeSSE(response.body, (event: StreamEvent) => {
        if (event.type === 'delta' && event.text) {
          callbacks.onDelta?.(event.text);
        } else if (event.type === 'complete') {
          callbacks.onComplete?.(parseCompleteEvent(event));
        } else if (event.type === 'error') {
          callbacks.onError?.(event.detail || 'Assistant stream reported an error');
        }
      });
    } catch (error) {
      if (!actualSignal?.aborted) {
        callbacks.onError?.(error instanceof Error ? error.message : String(error));
      }
    }
  }
}

export const assistantClient = new AssistantClient();
