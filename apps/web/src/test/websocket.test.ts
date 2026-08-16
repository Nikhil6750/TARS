import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { TARSWebSocketClient } from '../services/websocket';
import { TARSTradingEvent } from '../types/trading-event';

class MockWebSocket {
  public static instances: MockWebSocket[] = [];
  public url: string;
  public readyState: number = 0; // CONNECTING
  public onopen: (() => void) | null = null;
  public onmessage: ((evt: { data: string }) => void) | null = null;
  public onerror: ((evt: unknown) => void) | null = null;
  public onclose: ((evt: { code: number; reason: string }) => void) | null = null;
  public sentMessages: string[] = [];

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
    setTimeout(() => {
      this.readyState = 1; // OPEN
      this.onopen?.();
    }, 10);
  }

  public send(data: string) {
    this.sentMessages.push(data);
  }

  public close(code = 1000, reason = '') {
    this.readyState = 3; // CLOSED
    this.onclose?.({ code, reason });
  }

  public triggerMessage(data: unknown) {
    const payload = typeof data === 'string' ? data : JSON.stringify(data);
    this.onmessage?.({ data: payload });
  }
}

describe('TARSWebSocketClient', () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    (global as unknown as { WebSocket: typeof MockWebSocket }).WebSocket = MockWebSocket;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('connects to configured endpoint and transitions status to connected', async () => {
    const client = new TARSWebSocketClient('ws://127.0.0.1:8000/ws/events');
    const statuses: string[] = [];
    client.onConnectionChange((status) => statuses.push(status));

    client.connect();
    expect(client.getStatus()).toBe('connecting');

    await new Promise((r) => setTimeout(r, 20));
    expect(client.getStatus()).toBe('connected');
    expect(statuses).toContain('connected');

    client.disconnect();
    expect(client.getStatus()).toBe('offline');
  });

  it('receives, validates, and emits canonical trading events', async () => {
    const client = new TARSWebSocketClient('ws://127.0.0.1:8000/ws/events');
    const receivedEvents: TARSTradingEvent[] = [];
    client.onTradingEvent((evt) => receivedEvents.push(evt));

    client.connect();
    await new Promise((r) => setTimeout(r, 20));

    const wsInstance = MockWebSocket.instances[0];
    const validEvent: TARSTradingEvent = {
      schema_version: '1.0.0',
      event_id: '12345678-1234-1234-1234-123456789abc',
      timestamp: new Date().toISOString(),
      source: 'mock',
      symbol: 'NQ',
      state: 'SETUP_VALID',
      validation_status: 'VALID',
      direction: 'SHORT',
      entry: 20450.0,
      stop_loss: 20500.0,
      take_profit: 20300.0,
      risk_reward: 3.0
    };

    wsInstance.triggerMessage(validEvent);

    expect(receivedEvents.length).toBe(1);
    expect(receivedEvents[0].symbol).toBe('NQ');
    expect(receivedEvents[0].risk_reward).toBe(3.0);
  });

  it('catches and reports schema violations on malformed events', async () => {
    const client = new TARSWebSocketClient('ws://127.0.0.1:8000/ws/events');
    const protocolErrors: Array<{ title: string; errors: string[] }> = [];
    client.onProtocolError((title, errors) => protocolErrors.push({ title, errors }));

    client.connect();
    await new Promise((r) => setTimeout(r, 20));

    const wsInstance = MockWebSocket.instances[0];
    // Malformed event with illegal extra property and wrong schema_version
    wsInstance.triggerMessage({
      schema_version: '0.9.0',
      symbol: 'ES',
      state: 'SETUP_VALID',
      fake_ai_confidence: 99.9
    });

    expect(protocolErrors.length).toBe(1);
    expect(protocolErrors[0].title).toContain('Schema Validation Failed');
  });
});
