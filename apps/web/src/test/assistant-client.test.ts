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

describe('AssistantClient barge-in cancellation', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('passes the AbortSignal through to fetch', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(sseResponse([]));
    const controller = new AbortController();

    await assistantClient.streamQuery('hi', 'conv1', 'http://api', {}, controller.signal);

    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringContaining('/assistant/query/stream'),
      expect.objectContaining({ signal: controller.signal })
    );
  });

  it('does not report an error when the request is aborted mid-stream', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => {
      const err = new DOMException('The operation was aborted.', 'AbortError');
      return Promise.reject(err);
    });
    const controller = new AbortController();
    controller.abort();
    const onError = vi.fn();

    await assistantClient.streamQuery('hi', 'conv1', 'http://api', { onError }, controller.signal);

    expect(onError).not.toHaveBeenCalled();
  });

  it('still reports real network errors when not aborted', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('network down'));
    const onError = vi.fn();

    await assistantClient.streamQuery('hi', 'conv1', 'http://api', { onError });

    expect(onError).toHaveBeenCalledWith('network down');
  });

  it('delivers delta/complete events from a live stream', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      sseResponse([
        'data: {"type":"delta","text":"Hel"}\n\n',
        'data: {"type":"delta","text":"lo"}\n\n',
        'data: {"type":"complete","message":{"content":"Hello"}}\n\n',
      ])
    );
    const onDelta = vi.fn();
    const onComplete = vi.fn();

    await assistantClient.streamQuery('hi', 'conv1', 'http://api', { onDelta, onComplete });

    expect(onDelta.mock.calls.map((c) => c[0])).toEqual(['Hel', 'lo']);
    expect(onComplete).toHaveBeenCalledWith({ content: 'Hello' });
  });
});
