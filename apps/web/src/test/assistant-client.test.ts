import { describe, it, expect, vi, afterEach } from 'vitest';
import { assistantClient } from '../runtime/AssistantClient';

function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(stream, { status: 200 });
}

describe('AssistantClient presentation contracts & streaming', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('passes the AbortSignal and telemetry header through to fetch', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(sseResponse([]));
    const controller = new AbortController();

    await assistantClient.streamQuery('hi', 'conv1', 'http://api', {}, 'voice-trace-123', controller.signal);

    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringContaining('/assistant/query/stream'),
      expect.objectContaining({
        signal: controller.signal,
        headers: expect.objectContaining({
          'X-TARS-Voice-Turn-ID': 'voice-trace-123',
        }),
      })
    );
  });

  it('delivers delta and complete events with display_text and speech_text', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      sseResponse([
        'data: {"type":"delta","text":"### Market"}\n\n',
        'data: {"type":"delta","text":" Overview"}\n\n',
        'data: {"type":"complete","display_text":"### Market Overview\\n- Support holding","speech_text":"Market Overview. Support holding.","message":{"content":"### Market Overview"}}\n\n',
      ])
    );
    const onDelta = vi.fn();
    const onComplete = vi.fn();

    await assistantClient.streamQuery('hi', 'conv1', 'http://api', { onDelta, onComplete });

    expect(onDelta.mock.calls.map((c) => c[0])).toEqual(['### Market', ' Overview']);
    expect(onComplete).toHaveBeenCalledWith(
      expect.objectContaining({
        display_text: '### Market Overview\n- Support holding',
        speech_text: 'Market Overview. Support holding.',
        message: expect.objectContaining({ content: '### Market Overview' }),
      })
    );
  });

  it('assistantClient.query consumes /api/v2/assistant/query presentation envelope', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          message: {
            schema_version: '1.0.0',
            message_id: 'msg-1',
            conversation_id: 'conv-1',
            timestamp: new Date().toISOString(),
            role: 'assistant',
            content: 'Formatted **bold** answer',
            input_mode: 'text',
          },
          display_text: 'Formatted **bold** answer',
          speech_text: 'Formatted bold answer',
          quality: { directness: true, speech_suitability: true },
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    );

    const result = await assistantClient.query('what is risk?', 'conv-1', 'http://api', 'voice-turn-1');

    expect(result.display_text).toBe('Formatted **bold** answer');
    expect(result.speech_text).toBe('Formatted bold answer');
    expect(result.quality?.speech_suitability).toBe(true);
  });

  it('does not report an error when the request is aborted mid-stream', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => {
      const err = new DOMException('The operation was aborted.', 'AbortError');
      return Promise.reject(err);
    });
    const controller = new AbortController();
    controller.abort();
    const onError = vi.fn();

    await assistantClient.streamQuery('hi', 'conv1', 'http://api', { onError }, undefined, controller.signal);

    expect(onError).not.toHaveBeenCalled();
  });
});
