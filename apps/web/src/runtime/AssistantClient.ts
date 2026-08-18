/**
 * Streaming client for /api/v1/assistant/query/stream -- the voice-first
 * runtime's normal conversation path. Never waits for the full reply
 * before showing text: each `delta` event is handed to the caller as it
 * arrives so the panel and TTS can start reacting immediately.
 */
import { consumeSSE, StreamEvent } from './sse';

export interface AssistantStreamCallbacks {
  onDelta?: (text: string) => void;
  onComplete?: (message: Record<string, unknown> | undefined) => void;
  onError?: (detail: string) => void;
}

export class AssistantClient {
  public async streamQuery(
    text: string,
    conversationId: string | undefined,
    apiEndpoint: string,
    callbacks: AssistantStreamCallbacks
  ): Promise<void> {
    let res: Response;
    try {
      res = await fetch(`${apiEndpoint}/api/v1/assistant/query/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, conversation_id: conversationId }),
      });
    } catch (err) {
      callbacks.onError?.(err instanceof Error ? err.message : String(err));
      return;
    }

    if (!res.ok || !res.body) {
      callbacks.onError?.(`Assistant stream request failed with status ${res.status}`);
      return;
    }

    await consumeSSE(res.body, (event: StreamEvent) => {
      if (event.type === 'delta' && event.text) {
        callbacks.onDelta?.(event.text);
      } else if (event.type === 'complete') {
        callbacks.onComplete?.(event.message as Record<string, unknown> | undefined);
      } else if (event.type === 'error') {
        callbacks.onError?.(event.detail || 'Assistant stream reported an error');
      }
    });
  }
}

export const assistantClient = new AssistantClient();
