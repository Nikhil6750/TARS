/**
 * UI State & Companion Application Types
 */

import { TARSTradingEvent } from './trading-event';
import { TARSAssistantMessage } from './assistant-message';

export type CompanionVisualState =
  | 'IDLE'
  | 'LISTENING'
  | 'THINKING'
  | 'SPEAKING'
  | 'ALERT'
  | 'WARNING';

export type ActiveTab =
  | 'companion'
  | 'setups'
  | 'alerts'
  | 'chat'
  | 'voice'
  | 'memory'
  | 'system'
  | 'settings';

export type ConnectionStatus =
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'offline'
  | 'error';

export interface ConnectionState {
  status: ConnectionStatus;
  url: string;
  reconnectAttempts: number;
  lastConnectedAt?: string;
  lastHeartbeatAt?: string;
  latencyMs?: number;
  errorMessage?: string;
}

export interface AppSettings {
  serverEndpoint: string; // e.g. ws://localhost:8000/ws/events or wss://tars.tailscale.net/ws/events
  apiEndpoint: string;    // e.g. http://localhost:8000 or https://tars.tailscale.net
  audioEnabled: boolean;
  ttsVoice: string;
  speechRate: number;
  speechVolume: number;
  compactMode: boolean;
  hapticFeedback: boolean;
  mockGeneratorActive: boolean;
  mockIntervalSeconds: number;
  theme: 'dark-terminal' | 'cyber-amber' | 'deep-void' | 'emerald-matrix';
  autostartEnabled?: boolean;
  closeToTray?: boolean;
  globalSummonHotkey?: string;
  globalPttHotkey?: string;
}

export interface MemoryItem {
  id: string;
  type: 'operational_state' | 'conversation_memory' | 'research_knowledge' | 'journal_reference';
  title: string;
  content: string;
  source: string;
  timestamp: string;
  tags: string[];
}

export interface ActiveSetupFilter {
  symbolQuery: string;
  stateFilter: string;
  directionFilter: string;
  validationFilter: string;
}

export interface WebSocketInboundMessage {
  type: 'trading_event' | 'assistant_message' | 'companion_state' | 'ping' | 'pong';
  payload: TARSTradingEvent | TARSAssistantMessage | { state: CompanionVisualState; reason?: string } | Record<string, unknown>;
}
