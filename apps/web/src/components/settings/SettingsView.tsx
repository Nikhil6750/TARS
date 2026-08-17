import React from 'react';
import {
  Settings,
  Volume2,
  Bell,
  Play,
  Radio
} from 'lucide-react';
import { AppSettings } from '../../types/companion';
import { sendNotification, requestNotificationPermission } from '../../services/notifications';

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
          Configure audio synthesizers, mock generator cadence, notification channels, and companion layouts.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
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
