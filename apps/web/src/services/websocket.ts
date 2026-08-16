/**
 * Typed Real-Time WebSocket Client for TARS
 * Strictly validates all inbound events against canonical contracts.
 * Handles reconnects with exponential backoff & jitter, latency measurement, and endpoint switching.
 */

import { TARSTradingEvent } from '../types/trading-event';
import { TARSAssistantMessage } from '../types/assistant-message';
import { ConnectionStatus, CompanionVisualState } from '../types/companion';
import { validateTradingEvent, validateAssistantMessage } from '../contracts/validator';

export type TradingEventListener = (event: TARSTradingEvent) => void;
export type AssistantMessageListener = (message: TARSAssistantMessage) => void;
export type CompanionStateListener = (state: CompanionVisualState, reason?: string) => void;
export type ConnectionChangeListener = (status: ConnectionStatus, latencyMs?: number, error?: string) => void;
export type ProtocolErrorListener = (errTitle: string, details: string[]) => void;

export class TARSWebSocketClient {
  private ws: WebSocket | null = null;
  private url: string;
  private status: ConnectionStatus = 'offline';
  private reconnectAttempts = 0;
  private maxReconnectDelayMs = 15000;
  private baseReconnectDelayMs = 1000;
  private reconnectTimer: number | null = null;
  private pingIntervalTimer: number | null = null;
  private lastPingSentAt = 0;
  private currentLatencyMs = 0;
  private intentionalClose = false;

  private tradingEventListeners = new Set<TradingEventListener>();
  private assistantMessageListeners = new Set<AssistantMessageListener>();
  private companionStateListeners = new Set<CompanionStateListener>();
  private connectionChangeListeners = new Set<ConnectionChangeListener>();
  private protocolErrorListeners = new Set<ProtocolErrorListener>();

  constructor(initialUrl: string) {
    this.url = initialUrl;
  }

  public getStatus(): ConnectionStatus {
    return this.status;
  }

  public getLatency(): number {
    return this.currentLatencyMs;
  }

  public getUrl(): string {
    return this.url;
  }

  public setUrl(newUrl: string): void {
    if (this.url !== newUrl) {
      this.url = newUrl;
      if (this.status === 'connected' || this.status === 'connecting' || this.status === 'reconnecting') {
        this.disconnect();
        this.connect();
      }
    }
  }

  public connect(): void {
    this.intentionalClose = false;
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }

    this.setStatus(this.reconnectAttempts > 0 ? 'reconnecting' : 'connecting');

    try {
      this.ws = new WebSocket(this.url);
    } catch (err) {
      this.handleCloseOrError(`Failed to instantiate WebSocket: ${err instanceof Error ? err.message : String(err)}`);
      return;
    }

    this.ws.onopen = () => {
      this.reconnectAttempts = 0;
      this.setStatus('connected');
      this.startPingHeartbeat();
    };

    this.ws.onmessage = (evt: MessageEvent) => {
      this.handleIncomingMessage(evt.data);
    };

    this.ws.onerror = (evt: Event) => {
      console.warn('[TARS WebSocket] Error occurred:', evt);
    };

    this.ws.onclose = (evt: CloseEvent) => {
      if (!this.intentionalClose) {
        this.handleCloseOrError(`Connection closed (code: ${evt.code}, reason: ${evt.reason || 'none'})`);
      } else {
        this.setStatus('offline');
      }
    };
  }

  public disconnect(): void {
    this.intentionalClose = true;
    this.clearTimers();
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this.setStatus('offline');
  }

  public send(data: Record<string, unknown> | string): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      console.warn('[TARS WebSocket] Cannot send message; socket is not OPEN');
      return false;
    }

    try {
      const payload = typeof data === 'string' ? data : JSON.stringify(data);
      this.ws.send(payload);
      return true;
    } catch (err) {
      console.error('[TARS WebSocket] Send failure:', err);
      return false;
    }
  }

  private handleIncomingMessage(rawText: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(rawText);
    } catch {
      this.notifyProtocolError('Malformed JSON Received', [`Could not parse incoming WebSocket message: "${rawText.slice(0, 80)}..."`]);
      return;
    }

    if (typeof parsed !== 'object' || parsed === null) {
      this.notifyProtocolError('Invalid Message Payload', ['Incoming payload is not an object']);
      return;
    }

    const msg = parsed as Record<string, unknown>;

    // Heartbeat pong handling
    if (msg.type === 'pong') {
      if (this.lastPingSentAt > 0) {
        this.currentLatencyMs = Math.max(1, Date.now() - this.lastPingSentAt);
        this.notifyConnectionChange();
      }
      return;
    }

    // Active snapshot handling from backend on connection
    if (msg.type === 'active_snapshot' && Array.isArray(msg.events)) {
      msg.events.forEach((evt: unknown) => {
        const validation = validateTradingEvent(evt);
        if (validation.success) {
          this.tradingEventListeners.forEach((listener) => listener(validation.data));
        }
      });
      return;
    }

    // Direct trading event or wrapped trading event
    const eventCandidate = msg.type === 'trading_event' && msg.payload ? msg.payload : msg;

    // Check if looks like a trading event
    if ('state' in (eventCandidate as object) && 'symbol' in (eventCandidate as object)) {
      const validation = validateTradingEvent(eventCandidate);
      if (validation.success) {
        this.tradingEventListeners.forEach((listener) => listener(validation.data));
      } else {
        this.notifyProtocolError('Trading Event Schema Validation Failed', validation.errors);
      }
      return;
    }

    // Direct assistant message or wrapped
    const assistantCandidate = msg.type === 'assistant_message' && msg.payload ? msg.payload : msg;
    if ('role' in (assistantCandidate as object) && 'content' in (assistantCandidate as object)) {
      const validation = validateAssistantMessage(assistantCandidate);
      if (validation.success) {
        this.assistantMessageListeners.forEach((listener) => listener(validation.data));
      } else {
        this.notifyProtocolError('Assistant Message Schema Validation Failed', validation.errors);
      }
      return;
    }

    // Companion State updates (e.g. state: "LISTENING" | "THINKING" | "SPEAKING")
    if (msg.type === 'companion_state' && msg.state) {
      const visualState = String(msg.state) as CompanionVisualState;
      const reason = typeof msg.reason === 'string' ? msg.reason : undefined;
      this.companionStateListeners.forEach((listener) => listener(visualState, reason));
      return;
    }
  }

  private handleCloseOrError(errDetail: string): void {
    this.clearTimers();
    this.setStatus(this.reconnectAttempts > 0 ? 'reconnecting' : 'offline', errDetail);

    if (!this.intentionalClose) {
      this.reconnectAttempts++;
      // Exponential backoff with jitter
      const delay = Math.min(
        this.maxReconnectDelayMs,
        this.baseReconnectDelayMs * Math.pow(1.5, this.reconnectAttempts) + Math.random() * 500
      );
      this.reconnectTimer = window.setTimeout(() => {
        this.connect();
      }, delay);
    }
  }

  private startPingHeartbeat(): void {
    this.clearPingHeartbeat();
    this.pingIntervalTimer = window.setInterval(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.lastPingSentAt = Date.now();
        this.send({ type: 'ping', timestamp: new Date().toISOString() });
      }
    }, 10000);
  }

  private clearPingHeartbeat(): void {
    if (this.pingIntervalTimer !== null) {
      clearInterval(this.pingIntervalTimer);
      this.pingIntervalTimer = null;
    }
  }

  private clearTimers(): void {
    this.clearPingHeartbeat();
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private setStatus(newStatus: ConnectionStatus, errorMsg?: string): void {
    this.status = newStatus;
    this.notifyConnectionChange(errorMsg);
  }

  private notifyConnectionChange(errorMsg?: string): void {
    this.connectionChangeListeners.forEach((listener) => {
      listener(this.status, this.currentLatencyMs, errorMsg);
    });
  }

  private notifyProtocolError(title: string, errors: string[]): void {
    console.error(`[TARS Contract Violation] ${title}:`, errors);
    this.protocolErrorListeners.forEach((listener) => listener(title, errors));
  }

  // Subscription methods
  public onTradingEvent(listener: TradingEventListener): () => void {
    this.tradingEventListeners.add(listener);
    return () => this.tradingEventListeners.delete(listener);
  }

  public onAssistantMessage(listener: AssistantMessageListener): () => void {
    this.assistantMessageListeners.add(listener);
    return () => this.assistantMessageListeners.delete(listener);
  }

  public onCompanionState(listener: CompanionStateListener): () => void {
    this.companionStateListeners.add(listener);
    return () => this.companionStateListeners.delete(listener);
  }

  public onConnectionChange(listener: ConnectionChangeListener): () => void {
    this.connectionChangeListeners.add(listener);
    return () => this.connectionChangeListeners.delete(listener);
  }

  public onProtocolError(listener: ProtocolErrorListener): () => void {
    this.protocolErrorListeners.add(listener);
    return () => this.protocolErrorListeners.delete(listener);
  }
}
