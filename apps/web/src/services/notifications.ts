/**
 * Unified Notifications Layer (Tauri Native + Web Notifications)
 * Per ADR-015: Notifications are outputs of the event stream only, never decision makers.
 */

import { isTauri } from './tauri';

export interface TARSNotificationOptions {
  title: string;
  body: string;
  icon?: string;
}

export async function requestNotificationPermission(): Promise<boolean> {
  if (isTauri()) {
    try {
      const { isPermissionGranted, requestPermission } = await import('@tauri-apps/plugin-notification');
      let granted = await isPermissionGranted();
      if (!granted) {
        const permission = await requestPermission();
        granted = permission === 'granted';
      }
      return granted;
    } catch (err) {
      console.warn('Tauri notification permission check failed:', err);
    }
  }

  if (typeof window !== 'undefined' && 'Notification' in window) {
    if (Notification.permission === 'granted') {
      return true;
    }
    if (Notification.permission !== 'denied') {
      const permission = await Notification.requestPermission();
      return permission === 'granted';
    }
  }

  return false;
}

export async function sendNotification(options: TARSNotificationOptions): Promise<void> {
  if (isTauri()) {
    try {
      const { sendNotification: tauriSend, isPermissionGranted } = await import('@tauri-apps/plugin-notification');
      const granted = await isPermissionGranted();
      if (granted) {
        tauriSend({
          title: options.title,
          body: options.body,
        });
        return;
      }
    } catch (err) {
      console.warn('Failed to send Tauri notification:', err);
    }
  }

  if (typeof window !== 'undefined' && 'Notification' in window && Notification.permission === 'granted') {
    try {
      new Notification(options.title, {
        body: options.body,
        icon: options.icon || '/icons/icon-192.svg',
        badge: '/icons/icon-192.svg'
      });
    } catch (err) {
      console.warn('Failed to send Web Notification:', err);
    }
  }
}
