import React from 'react';
import {
  Activity,
  Layers,
  Bell,
  MessageSquare,
  Mic,
  Cpu,
  Settings,
  Database,
  Minimize2,
  Maximize2,
  X,
  Radio
} from 'lucide-react';
import { ActiveTab, ConnectionStatus } from '../../types/companion';
import { isTauri, minimizeWindow, toggleMaximizeWindow, closeWindow } from '../../services/tauri';

interface DesktopHeaderProps {
  activeTab: ActiveTab;
  setActiveTab: (tab: ActiveTab) => void;
  connectionStatus: ConnectionStatus;
  latencyMs: number;
  compactMode: boolean;
  setCompactMode: (compact: boolean) => void;
  activeSetupsCount: number;
  unreadAlertsCount: number;
}

export const DesktopHeader: React.FC<DesktopHeaderProps> = ({
  activeTab,
  setActiveTab,
  connectionStatus,
  latencyMs,
  compactMode,
  setCompactMode,
  activeSetupsCount,
  unreadAlertsCount,
}) => {
  const isNative = isTauri();

  const statusColorMap: Record<ConnectionStatus, { bg: string; text: string; label: string }> = {
    connected: { bg: '#00ff66', text: '#00ff66', label: 'LIVE' },
    connecting: { bg: '#00f0ff', text: '#00f0ff', label: 'CONNECTING' },
    reconnecting: { bg: '#ffb700', text: '#ffb700', label: 'RECONNECTING' },
    offline: { bg: '#ff3366', text: '#ff3366', label: 'OFFLINE' },
    error: { bg: '#ff3366', text: '#ff3366', label: 'ERROR' },
  };

  const statusInfo = statusColorMap[connectionStatus] || statusColorMap.offline;

  const navItems: Array<{ tab: ActiveTab; label: string; icon: React.ComponentType<{ className?: string }>; badge?: number }> = [
    { tab: 'companion', label: 'Companion', icon: Activity },
    { tab: 'setups', label: 'Setups', icon: Layers, badge: activeSetupsCount },
    { tab: 'alerts', label: 'Alerts', icon: Bell, badge: unreadAlertsCount },
    { tab: 'chat', label: 'Ask TARS', icon: MessageSquare },
    { tab: 'voice', label: 'Voice', icon: Mic },
    { tab: 'memory', label: 'Memory & Research', icon: Database },
    { tab: 'system', label: 'System', icon: Cpu },
    { tab: 'settings', label: 'Settings', icon: Settings },
  ];

  return (
    <header className="w-full bg-[#060a12]/90 backdrop-blur-md border-b border-cyan-500/15 select-none z-40">
      <div className="flex items-center justify-between px-4 py-2">
        {/* Left: Brand + Status Pill */}
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-md bg-[#0a1424] border border-cyan-500/40 flex items-center justify-center font-display-title font-bold text-cyan-400 text-sm glow-cyan">
              T
            </div>
            <div>
              <div className="font-display-title font-bold text-sm tracking-wider text-slate-100 flex items-center gap-1.5">
                TARS <span className="text-[10px] text-cyan-400 font-numeric px-1 py-0.5 rounded bg-cyan-950/60 border border-cyan-500/30">V1</span>
              </div>
              <div className="text-[10px] text-slate-400 font-mono tracking-tight">QUANT TRADING COMPANION</div>
            </div>
          </div>

          {/* Connection Status Pill */}
          <div className="flex items-center gap-2 px-2.5 py-1 rounded-full bg-[#091220] border border-slate-700/60">
            <span
              className="w-2 h-2 rounded-full"
              style={{
                backgroundColor: statusInfo.bg,
                boxShadow: `0 0 8px ${statusInfo.bg}`
              }}
            />
            <span className="text-[11px] font-mono font-semibold" style={{ color: statusInfo.text }}>
              {statusInfo.label}
            </span>
            {connectionStatus === 'connected' && latencyMs > 0 && (
              <span className="text-[10px] text-slate-400 font-numeric">
                {latencyMs}ms
              </span>
            )}
          </div>
        </div>

        {/* Center: Main Navigation Tabs */}
        <nav className="hidden lg:flex items-center gap-1 bg-[#091220]/80 p-1 rounded-lg border border-cyan-500/10">
          {navItems.map((item) => {
            const Icon = item.icon;
            const isActive = activeTab === item.tab;
            return (
              <button
                key={item.tab}
                onClick={() => setActiveTab(item.tab)}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-all duration-200 cursor-pointer ${
                  isActive
                    ? 'bg-cyan-500/20 text-cyan-300 border border-cyan-500/30 shadow-[0_0_12px_rgba(0,240,255,0.2)]'
                    : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/40'
                }`}
              >
                <Icon className="w-3.5 h-3.5" />
                <span>{item.label}</span>
                {item.badge !== undefined && item.badge > 0 && (
                  <span className="ml-1 px-1.5 py-0.2 rounded-full text-[10px] font-mono bg-cyan-500/30 text-cyan-200">
                    {item.badge}
                  </span>
                )}
              </button>
            );
          })}
        </nav>

        {/* Right: Actions + Window Controls */}
        <div className="flex items-center gap-2">
          {/* Compact Mode Toggle */}
          <button
            onClick={() => setCompactMode(!compactMode)}
            title={compactMode ? 'Expand Full View' : 'Enter Compact Companion HUD'}
            className={`p-1.5 rounded-md text-xs border transition-colors cursor-pointer flex items-center gap-1 ${
              compactMode
                ? 'bg-amber-500/20 text-amber-300 border-amber-500/40 shadow-[0_0_10px_rgba(255,183,0,0.3)]'
                : 'bg-slate-900 text-slate-400 border-slate-700 hover:text-slate-200 hover:border-slate-600'
            }`}
          >
            <Radio className="w-3.5 h-3.5" />
            <span className="hidden sm:inline text-[11px] font-mono">{compactMode ? 'HUD ON' : 'HUD'}</span>
          </button>

          {/* Tauri Native Window Controls (if inside desktop) */}
          {isNative && (
            <div className="flex items-center ml-2 border-l border-slate-800 pl-2 gap-1">
              <button
                onClick={minimizeWindow}
                className="p-1.5 rounded hover:bg-slate-800 text-slate-400 hover:text-slate-100 transition-colors"
                title="Minimize Window"
              >
                <Minimize2 className="w-3.5 h-3.5" />
              </button>
              <button
                onClick={toggleMaximizeWindow}
                className="p-1.5 rounded hover:bg-slate-800 text-slate-400 hover:text-slate-100 transition-colors"
                title="Maximize Window"
              >
                <Maximize2 className="w-3.5 h-3.5" />
              </button>
              <button
                onClick={closeWindow}
                className="p-1.5 rounded hover:bg-ruby-600/30 text-slate-400 hover:text-ruby-400 transition-colors"
                title="Close Window"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          )}
        </div>
      </div>
    </header>
  );
};
