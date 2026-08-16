/**
 * Local Storage Persistence Manager for TARS Companion
 */

import { AppSettings } from '../types/companion';
import { TARSTradingEvent } from '../types/trading-event';
import { TARSAssistantMessage } from '../types/assistant-message';

const SETTINGS_KEY = 'tars_settings_v1';
const ALERTS_KEY = 'tars_alerts_history_v1';
const CHAT_KEY = 'tars_chat_history_v1';

export const DEFAULT_SETTINGS: AppSettings = {
  serverEndpoint: 'ws://127.0.0.1:8000/ws/events',
  apiEndpoint: 'http://127.0.0.1:8000',
  audioEnabled: true,
  ttsVoice: 'default',
  speechRate: 1.0,
  speechVolume: 0.9,
  compactMode: false,
  hapticFeedback: true,
  mockGeneratorActive: true, // Active by default for standalone demo & testing
  mockIntervalSeconds: 8,
  theme: 'dark-terminal'
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
