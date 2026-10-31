import { afterEach, describe, expect, it } from 'vitest';
import { backendHttpUrl, backendWsUrl, followShellEndpoint } from '../runtime/backendUrl';

const globals = globalThis as { __TARS_BACKEND_URL__?: string };

describe('backend address resolution', () => {
  afterEach(() => {
    delete globals.__TARS_BACKEND_URL__;
  });

  it('defaults to the historical address outside the shell', () => {
    expect(backendHttpUrl()).toBe('http://127.0.0.1:8000');
    expect(backendWsUrl('/ws/events')).toBe('ws://127.0.0.1:8000/ws/events');
  });

  it('uses the address the desktop shell injected', () => {
    globals.__TARS_BACKEND_URL__ = 'http://127.0.0.1:8123/';
    expect(backendHttpUrl()).toBe('http://127.0.0.1:8123');
    expect(backendWsUrl('/api/v1/market/ws')).toBe('ws://127.0.0.1:8123/api/v1/market/ws');
  });

  it('moves a remembered loopback endpoint to the port the shell manages', () => {
    globals.__TARS_BACKEND_URL__ = 'http://127.0.0.1:8123';
    expect(followShellEndpoint('http://127.0.0.1:8000', 'http')).toBe('http://127.0.0.1:8123');
    expect(followShellEndpoint('ws://localhost:8000/ws/events', 'ws')).toBe('ws://127.0.0.1:8123/ws/events');
  });

  it('never overrides a deliberately remote endpoint', () => {
    globals.__TARS_BACKEND_URL__ = 'http://127.0.0.1:8123';
    expect(followShellEndpoint('wss://tars.tailscale.net/ws/events', 'ws')).toBe('wss://tars.tailscale.net/ws/events');
    expect(followShellEndpoint('https://tars.tailscale.net', 'http')).toBe('https://tars.tailscale.net');
  });

  it('leaves saved settings alone outside the shell', () => {
    expect(followShellEndpoint('http://127.0.0.1:9999', 'http')).toBe('http://127.0.0.1:9999');
  });
});
