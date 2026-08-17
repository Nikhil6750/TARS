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

  it('handles real backend trading_event envelope with { type: "trading_event", event: payload }', async () => {
    const client = new TARSWebSocketClient('ws://127.0.0.1:8000/ws/events');
    const receivedEvents: TARSTradingEvent[] = [];
    client.onTradingEvent((evt) => receivedEvents.push(evt));

    client.connect();
    await new Promise((r) => setTimeout(r, 20));

    const wsInstance = MockWebSocket.instances[0];
    const backendTradingEventPayload: TARSTradingEvent = {
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

    // Real backend EventBus.broadcast envelope
    wsInstance.triggerMessage({
      type: 'trading_event',
      event: backendTradingEventPayload,
      active_state_change: 'add'
    });

    expect(receivedEvents.length).toBe(1);
    expect(receivedEvents[0].symbol).toBe('NQ');
    expect(receivedEvents[0].state).toBe('SETUP_VALID');
    expect(receivedEvents[0].risk_reward).toBe(3.0);
  });

  it('handles real backend active_snapshot hydration on connection', async () => {
    const client = new TARSWebSocketClient('ws://127.0.0.1:8000/ws/events');
    const snapshotEvents: TARSTradingEvent[] = [];
    const individualEvents: TARSTradingEvent[] = [];

    client.onActiveSnapshot((events) => snapshotEvents.push(...events));
    client.onTradingEvent((evt) => individualEvents.push(evt));

    client.connect();
    await new Promise((r) => setTimeout(r, 20));

    const wsInstance = MockWebSocket.instances[0];
    const setup1: TARSTradingEvent = {
      schema_version: '1.0.0',
      event_id: 'e1-1234-1234-1234-123456789abc',
      timestamp: new Date().toISOString(),
      source: 'mock',
      symbol: 'XAUUSD',
      state: 'SETUP_VALID',
      validation_status: 'VALID',
      direction: 'LONG',
      entry: 2684.50,
      stop_loss: 2676.00,
      take_profit: 2708.50,
      risk_reward: 2.82
    };

    const setup2: TARSTradingEvent = {
      schema_version: '1.0.0',
      event_id: 'e2-1234-1234-1234-123456789abc',
      timestamp: new Date().toISOString(),
      source: 'mock',
      symbol: 'ES',
      state: 'SETUP_DEVELOPING',
      validation_status: 'PENDING',
      direction: 'LONG',
      entry: 5880.0,
      stop_loss: 5865.0,
      take_profit: 5920.0,
      risk_reward: 2.67
    };

    wsInstance.triggerMessage({
      type: 'active_snapshot',
      events: [setup1, setup2]
    });

    expect(snapshotEvents.length).toBe(2);
    expect(snapshotEvents[0].symbol).toBe('XAUUSD');
    expect(snapshotEvents[1].symbol).toBe('ES');
    expect(individualEvents.length).toBe(2);
  });

  it('handles heartbeat ping/pong latency measurement', async () => {
    const client = new TARSWebSocketClient('ws://127.0.0.1:8000/ws/events');
    client.connect();
    await new Promise((r) => setTimeout(r, 20));

    const wsInstance = MockWebSocket.instances[0];
    wsInstance.triggerMessage({
      type: 'pong',
      timestamp: new Date().toISOString()
    });

    expect(client.getStatus()).toBe('connected');
  });

  it('supports explicit reconnect method', async () => {
    const client = new TARSWebSocketClient('ws://127.0.0.1:8000/ws/events');
    client.connect();
    await new Promise((r) => setTimeout(r, 20));
    expect(MockWebSocket.instances.length).toBe(1);

    client.reconnect();
    await new Promise((r) => setTimeout(r, 20));
    expect(MockWebSocket.instances.length).toBe(2);
    expect(client.getStatus()).toBe('connected');
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
      type: 'trading_event',
      event: {
        schema_version: '0.9.0',
        symbol: 'ES',
        state: 'SETUP_VALID',
        fake_ai_confidence: 99.9
      }
    });

    expect(protocolErrors.length).toBe(1);
    expect(protocolErrors[0].title).toContain('Schema Validation Failed');
  });
});
