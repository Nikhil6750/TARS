/**
 * UI State & Companion Application Types
 */

import { TARSTradingEvent } from './trading-event';
import { TARSAssistantMessage } from './assistant-message';

export type CompanionVisualState =
  | 'IDLE'
  | 'WAKE'
  | 'LISTENING'
  | 'THINKING'
  | 'SPEAKING'
  | 'ALERT'
  | 'WARNING';

/** Primary navigation: kept deliberately small -- TARS (the default,
 * chat-first assistant screen) / Workspace (the quant dashboard, demoted
 * to secondary) / Settings. */
export type ActiveTab = 'tars' | 'workspace' | 'settings';

/** Internal Workspace sections -- everything the old primary nav used to
 * expose directly now lives here instead. */
export type WorkspaceSection = 'companion' | 'setups' | 'alerts' | 'voice' | 'memory' | 'system';

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
