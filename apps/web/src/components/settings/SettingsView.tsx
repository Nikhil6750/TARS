import React, { useState, useEffect } from 'react';
import {
  Settings,
  Volume2,
  Bell,
  Play,
  Radio,
  Power,
  Keyboard,
  Shield,
  Layers,
} from 'lucide-react';
import { AppSettings } from '../../types/companion';
import { sendNotification, requestNotificationPermission } from '../../services/notifications';
import { nativeBridge } from '../../services/native-bridge';

interface SettingsViewProps {
  settings: AppSettings;
  onUpdateSettings: (newSettings: Partial<AppSettings>) => void;
  onTriggerMockEvent: () => void;
}

export const SettingsView: React.FC<SettingsViewProps> = ({
  settings,
  onUpdateSettings,
  onTriggerMockEvent
}) => {
  const [autostartActive, setAutostartActive] = useState<boolean>(settings.autostartEnabled ?? false);
  const [isUpdatingAutostart, setIsUpdatingAutostart] = useState(false);

  useEffect(() => {
    async function loadAutostart() {
      try {
        const enabled = await nativeBridge.getAutostartStatus();
        setAutostartActive(enabled);
      } catch (err) {
        console.warn('Could not query autostart status:', err);
      }
    }
    loadAutostart();
  }, []);

  const handleToggleAutostart = async (enabled: boolean) => {
    setIsUpdatingAutostart(true);
    try {
      const result = await nativeBridge.setAutostart(enabled);
      setAutostartActive(result);
      onUpdateSettings({ autostartEnabled: result });
    } catch (err) {
      console.error('Failed to set autostart:', err);
    } finally {
      setIsUpdatingAutostart(false);
    }
  };

  const handleTestNotification = async () => {
    const permitted = await requestNotificationPermission();
    if (permitted) {
      sendNotification({
        title: 'TARS Trading Setup Alert',
        body: 'XAUUSD (Gold) H4 Orderblock tap confirmed. Risk:Reward 2.82.',
      });
    } else {
      alert('Notification permissions are not enabled in your browser or desktop settings.');
    }
  };

  return (
    <div className="w-full h-full flex flex-col gap-4 p-3 md:p-6 overflow-y-auto max-w-4xl mx-auto">
      {/* Header */}
      <div className="pb-3 border-b border-cyan-500/20">
        <h1 className="text-base font-display-title font-bold text-slate-100 flex items-center gap-2">
          <Settings className="w-4 h-4 text-cyan-400" />
          TARS SYSTEM PREFERENCES
        </h1>
        <p className="text-[11px] font-mono text-slate-400">
          Configure Windows-wide native shell, autostart, global hotkeys, audio synthesizers, and companion layouts.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* Windows-wide Assistant Shell & Autostart (Wave 2A) */}
        <div className="glass-panel p-5 bg-[#081224]/90 flex flex-col justify-between border-cyan-500/30">
          <div>
            <div className="flex items-center justify-between pb-2 border-b border-slate-800">
              <span className="text-xs font-mono font-bold text-cyan-300 flex items-center gap-2">
                <Power className="w-4 h-4 text-cyan-400" />
                WINDOWS AUTOSTART (WAVE 2A)
              </span>
              <input
                type="checkbox"
                checked={autostartActive}
                disabled={isUpdatingAutostart}
                onChange={(e) => handleToggleAutostart(e.target.checked)}
                className="w-4 h-4 accent-cyan-400 cursor-pointer disabled:opacity-50"
              />
            </div>
            <p className="text-xs text-slate-400 mt-2 font-sans">
              Configures TARS to launch in the background upon Windows user login via the standard user Registry Run key (M2A Criterion 3: off by default).
            </p>

            <div className="mt-3 p-2 bg-[#040810] rounded border border-slate-800 font-mono text-[11px] space-y-1">
              <div className="flex items-center justify-between text-slate-400">
                <span>Registry Key:</span>
                <span className="text-cyan-400 text-[10px]">HKCU\Software\...\Run\TARS</span>
              </div>
              <div className="flex items-center justify-between text-slate-400">
                <span>Status:</span>
                <span className={autostartActive ? 'text-emerald-400 font-bold' : 'text-slate-500'}>
                  {autostartActive ? 'ENABLED' : 'DISABLED'}
                </span>
              </div>
            </div>
          </div>

          <div className="mt-3 pt-2 border-t border-slate-800 text-[11px] font-mono text-slate-400 flex items-center gap-1.5">
            <Shield className="w-3.5 h-3.5 text-cyan-400" />
            <span>Runs un-elevated in user session</span>
          </div>
        </div>

        {/* Global Hotkeys & Background Tray */}
        <div className="glass-panel p-5 bg-[#081224]/90 flex flex-col justify-between">
          <div>
            <div className="flex items-center justify-between pb-2 border-b border-slate-800">
              <span className="text-xs font-mono font-bold text-cyan-300 flex items-center gap-2">
                <Keyboard className="w-4 h-4 text-cyan-400" />
                GLOBAL SHORTCUTS & TRAY
              </span>
            </div>
            <p className="text-xs text-slate-400 mt-2 font-sans">
              Summon and interact with TARS from any active Windows application.
            </p>

            <div className="mt-3 space-y-2 font-mono text-xs">
              <div className="flex items-center justify-between p-2 bg-[#040810] rounded border border-slate-800">
                <span className="text-slate-400">Summon / Hide HUD:</span>
                <span className="px-2 py-0.5 bg-cyan-950 text-cyan-300 rounded border border-cyan-500/40 text-[11px] font-bold">
                  Ctrl+Shift+Space / Ctrl+Shift+T
                </span>
              </div>
              <div className="flex items-center justify-between p-2 bg-[#040810] rounded border border-slate-800">
                <span className="text-slate-400">Push-to-Talk (PTT):</span>
                <span className="px-2 py-0.5 bg-emerald-950 text-emerald-300 rounded border border-emerald-500/40 text-[11px] font-bold">
                  Ctrl+Shift+V
                </span>
              </div>
              <div className="flex items-center justify-between p-2 bg-[#040810] rounded border border-slate-800">
                <span className="text-slate-400">Close to Tray:</span>
                <span className="text-emerald-400 font-bold text-[11px]">ACTIVE (PERSISTENT)</span>
              </div>
            </div>
          </div>

          <div className="mt-3 pt-2 border-t border-slate-800 text-[11px] font-mono text-slate-400 flex items-center gap-1.5">
            <Layers className="w-3.5 h-3.5 text-cyan-400" />
            <span>Tray menu: Show HUD | Open Dashboard | Quit</span>
          </div>
        </div>

        {/* Mock Event Generator (Development Fixture) */}
        <div className="glass-panel p-5 bg-[#081224]/90 flex flex-col justify-between">
          <div>
            <div className="flex items-center justify-between pb-2 border-b border-slate-800">
              <span className="text-xs font-mono font-bold text-cyan-300 flex items-center gap-2">
                <Play className="w-4 h-4 text-cyan-400" />
                MOCK EVENT FIXTURE GENERATOR
              </span>
              <input
                type="checkbox"
                checked={settings.mockGeneratorActive}
                onChange={(e) => onUpdateSettings({ mockGeneratorActive: e.target.checked })}
                className="w-4 h-4 accent-cyan-400 cursor-pointer"
              />
            </div>
            <p className="text-xs text-slate-400 mt-2 font-sans">
              Emits canonical schema-compliant mock trading events and simulated confluences for local verification with zero backend dependencies.
            </p>

            {/* Interval Slider */}
            <div className="mt-4">
              <div className="flex items-center justify-between text-xs font-mono text-slate-300">
                <span>Simulation Interval:</span>
                <span className="text-cyan-400 font-bold">{settings.mockIntervalSeconds}s</span>
              </div>
              <input
                type="range"
                min="3"
                max="30"
                value={settings.mockIntervalSeconds}
                onChange={(e) => onUpdateSettings({ mockIntervalSeconds: Number(e.target.value) })}
                className="w-full mt-2 accent-cyan-400 cursor-pointer"
              />
            </div>
          </div>

          <button
            onClick={onTriggerMockEvent}
            className="mt-4 w-full py-2 rounded-lg bg-cyan-500/20 hover:bg-cyan-500/30 border border-cyan-500/40 text-cyan-300 text-xs font-mono font-bold transition-colors cursor-pointer"
          >
            Emit Manual Test Trading Event
          </button>
        </div>

        {/* Notifications */}
        <div className="glass-panel p-5 bg-[#081224]/90 flex flex-col justify-between">
          <div>
            <div className="flex items-center justify-between pb-2 border-b border-slate-800">
              <span className="text-xs font-mono font-bold text-cyan-300 flex items-center gap-2">
                <Bell className="w-4 h-4 text-cyan-400" />
                DESKTOP & WEB NOTIFICATIONS
              </span>
            </div>
            <p className="text-xs text-slate-400 mt-2 font-sans">
              Surfaces high-confluence setup alerts and risk warnings via native Tauri layer or PWA Web Push (ADR-015).
            </p>
          </div>

          <button
            onClick={handleTestNotification}
            className="mt-4 w-full py-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-mono transition-colors flex items-center justify-center gap-2 cursor-pointer"
          >
            <Bell className="w-3.5 h-3.5 text-cyan-400" />
            <span>Test Native Notification</span>
          </button>
        </div>

        {/* Speech / Audio Synthesis */}
        <div className="glass-panel p-5 bg-[#081224]/90">
          <div className="flex items-center justify-between pb-2 border-b border-slate-800">
            <span className="text-xs font-mono font-bold text-cyan-300 flex items-center gap-2">
              <Volume2 className="w-4 h-4 text-cyan-400" />
              VOICE & SPEECH SYNTHESIS
            </span>
          </div>

          <div className="space-y-3 mt-3 text-xs font-mono">
            <div>
              <div className="flex items-center justify-between text-slate-300">
                <span>Speech Volume:</span>
                <span className="text-cyan-400 font-bold">{Math.round(settings.speechVolume * 100)}%</span>
              </div>
              <input
                type="range"
                min="0"
                max="1"
                step="0.05"
                value={settings.speechVolume}
                onChange={(e) => onUpdateSettings({ speechVolume: Number(e.target.value) })}
                className="w-full mt-1 accent-cyan-400 cursor-pointer"
              />
            </div>

            <div>
              <div className="flex items-center justify-between text-slate-300">
                <span>Speech Cadence / Rate:</span>
                <span className="text-cyan-400 font-bold">{settings.speechRate}x</span>
              </div>
              <input
                type="range"
                min="0.75"
                max="1.5"
                step="0.05"
                value={settings.speechRate}
                onChange={(e) => onUpdateSettings({ speechRate: Number(e.target.value) })}
                className="w-full mt-1 accent-cyan-400 cursor-pointer"
              />
            </div>
          </div>
        </div>

        {/* Companion HUD Layout */}
        <div className="glass-panel p-5 bg-[#081224]/90 flex flex-col justify-between">
          <div>
            <div className="flex items-center justify-between pb-2 border-b border-slate-800">
              <span className="text-xs font-mono font-bold text-cyan-300 flex items-center gap-2">
                <Radio className="w-4 h-4 text-cyan-400" />
                COMPACT COMPANION HUD
              </span>
            </div>
            <p className="text-xs text-slate-400 mt-2 font-sans">
              Switch between the full workstation interface and the compact always-on-top companion widget for trading alongside charts.
            </p>
          </div>

          <button
            onClick={() => onUpdateSettings({ compactMode: !settings.compactMode })}
            className="mt-4 w-full py-2 rounded-lg bg-amber-500/20 hover:bg-amber-500/30 border border-amber-500/40 text-amber-300 text-xs font-mono font-bold transition-colors cursor-pointer"
          >
            {settings.compactMode ? 'Exit Compact HUD Mode' : 'Enter Compact HUD Mode'}
          </button>
        </div>
      </div>
    </div>
  );
};

