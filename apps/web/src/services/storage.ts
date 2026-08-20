/**
 * Local Storage Persistence Manager for TARS Companion
 */

import { AppSettings } from '../types/companion';
import { TARSTradingEvent } from '../types/trading-event';
import { TARSAssistantMessage } from '../types/assistant-message';

const SETTINGS_KEY = 'tars_settings_v1';
const ALERTS_KEY = 'tars_alerts_history_v1';
const CHAT_KEY = 'tars_chat_history_v1';
const SESSIONS_KEY = 'tars_chat_sessions_v1';

const metaEnv = typeof import.meta !== 'undefined' ? (import.meta as unknown as { env?: Record<string, string> }).env : undefined;
const envApiUrl = metaEnv?.VITE_TARS_API_URL ? String(metaEnv.VITE_TARS_API_URL) : 'http://127.0.0.1:8000';
const rawWs = metaEnv?.VITE_TARS_WS_URL ? String(metaEnv.VITE_TARS_WS_URL) : '';
const envWsUrl = rawWs
  ? (rawWs.endsWith('/ws/events') ? rawWs : rawWs.replace(/\/$/, '') + '/ws/events')
  : 'ws://127.0.0.1:8000/ws/events';

export const DEFAULT_SETTINGS: AppSettings = {
  serverEndpoint: envWsUrl,
  apiEndpoint: envApiUrl,
  audioEnabled: true,
  ttsVoice: 'default',
  speechRate: 1.0,
  speechVolume: 0.9,
  compactMode: true,
  hapticFeedback: true,
  mockGeneratorActive: false, // Default connected to real backend
  mockIntervalSeconds: 8,
  theme: 'dark-terminal',
  autostartEnabled: false,
  closeToTray: true,
  globalSummonHotkey: 'Ctrl+Shift+Space',
  globalPttHotkey: 'Ctrl+Shift+V'
};

export function loadSettings(): AppSettings {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (!raw) return DEFAULT_SETTINGS;
    return { ...DEFAULT_SETTINGS, ...JSON.parse(raw) };
  } catch {
    return DEFAULT_SETTINGS;
  }
}

export function saveSettings(settings: AppSettings): void {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  } catch (err) {
    console.error('Failed to save settings to localStorage:', err);
  }
}

export function loadStoredAlerts(): TARSTradingEvent[] {
  try {
    const raw = localStorage.getItem(ALERTS_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

export function saveStoredAlerts(alerts: TARSTradingEvent[]): void {
  try {
    // Keep last 200 alerts in local cache
    const slice = alerts.slice(-200);
    localStorage.setItem(ALERTS_KEY, JSON.stringify(slice));
  } catch (err) {
    console.error('Failed to save alerts to localStorage:', err);
  }
}

export function loadStoredChat(): TARSAssistantMessage[] {
  try {
    const raw = localStorage.getItem(CHAT_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

export function saveStoredChat(messages: TARSAssistantMessage[]): void {
  try {
    const slice = messages.slice(-100);
    localStorage.setItem(CHAT_KEY, JSON.stringify(slice));
  } catch (err) {
    console.error('Failed to save chat to localStorage:', err);
  }
}

export interface StoredChatSession {
  id: string;
  title: string;
  createdAt: string;
  messages: TARSAssistantMessage[];
}

export function loadStoredSessions(): StoredChatSession[] {
  try {
    const raw = localStorage.getItem(SESSIONS_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed) && parsed.length > 0) {
        return parsed;
      }
    }
    // Migrate existing single chat if available
    const existingMessages = loadStoredChat();
    if (existingMessages.length > 0) {
      const firstUserMsg = existingMessages.find((m) => m.role === 'user');
      const title = firstUserMsg ? firstUserMsg.content.slice(0, 30) : 'General Session';
      const initialSession: StoredChatSession = {
        id: existingMessages[0]?.conversation_id || crypto.randomUUID(),
        title,
        createdAt: existingMessages[0]?.timestamp || new Date().toISOString(),
        messages: existingMessages,
      };
      saveStoredSessions([initialSession]);
      return [initialSession];
    }
    return [];
  } catch {
    return [];
  }
}

export function saveStoredSessions(sessions: StoredChatSession[]): void {
  try {
    // Keep up to 50 sessions
    const slice = sessions.slice(0, 50);
    localStorage.setItem(SESSIONS_KEY, JSON.stringify(slice));
  } catch (err) {
    console.error('Failed to save sessions to localStorage:', err);
  }
}
