/**
 * Minimal Server-Sent-Events body reader shared by AssistantClient and
 * ChartAnalysisClient. Both backend streams use the same `data: {...}\n\n`
 * framing (see app/routers/assistant.py), so this is the one place that
 * knows how to split a fetch() ReadableStream into JSON events.
 */
export interface StreamEvent {
  type: string;
  text?: string;
  detail?: string;
  [key: string]: unknown;
}

export async function consumeSSE(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: StreamEvent) => void
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? '';
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed.startsWith('data:')) continue;
      const jsonStr = trimmed.slice(5).trim();
      if (!jsonStr) continue;
      try {
        onEvent(JSON.parse(jsonStr) as StreamEvent);
      } catch {
        // Malformed/partial frame -- skip rather than throw, next frame continues normally.
      }
    }
  }
}
