import React from 'react';
import {
  Activity,
  Layers,
  Bell,
  MessageSquare,
  Mic,
  Settings
} from 'lucide-react';
import { ActiveTab } from '../../types/companion';

interface MobileTabBarProps {
  activeTab: ActiveTab;
  setActiveTab: (tab: ActiveTab) => void;
  activeSetupsCount: number;
  unreadAlertsCount: number;
}

export const MobileTabBar: React.FC<MobileTabBarProps> = ({
  activeTab,
  setActiveTab,
  activeSetupsCount,
  unreadAlertsCount,
}) => {
  const tabs: Array<{ tab: ActiveTab; label: string; icon: React.ComponentType<{ className?: string }>; badge?: number }> = [
    { tab: 'companion', label: 'TARS', icon: Activity },
    { tab: 'setups', label: 'Setups', icon: Layers, badge: activeSetupsCount },
    { tab: 'voice', label: 'Voice', icon: Mic },
    { tab: 'chat', label: 'Chat', icon: MessageSquare },
    { tab: 'alerts', label: 'Alerts', icon: Bell, badge: unreadAlertsCount },
    { tab: 'settings', label: 'Config', icon: Settings },
  ];

  return (
    <nav className="lg:hidden fixed bottom-0 left-0 right-0 z-50 bg-[#060a12]/95 backdrop-blur-xl border-t border-cyan-500/20 safe-bottom">
      <div className="flex items-center justify-around px-2 py-1">
        {tabs.map((item) => {
          const Icon = item.icon;
          const isActive = activeTab === item.tab;
          return (
            <button
              key={item.tab}
              onClick={() => {
                if (typeof navigator !== 'undefined' && navigator.vibrate) {
                  navigator.vibrate(10);
                }
                setActiveTab(item.tab);
              }}
              className={`flex flex-col items-center justify-center py-1 px-2 rounded-lg touch-target transition-all ${
                isActive
                  ? 'text-cyan-400 font-semibold'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              <div className="relative">
                <Icon className={`w-5 h-5 transition-transform ${isActive ? 'scale-110' : ''}`} />
                {item.badge !== undefined && item.badge > 0 && (
                  <span className="absolute -top-1.5 -right-2.5 px-1 min-w-4 h-4 rounded-full text-[9px] font-mono bg-cyan-500 text-slate-950 font-bold flex items-center justify-center shadow-[0_0_8px_rgba(0,240,255,0.6)]">
                    {item.badge}
                  </span>
                )}
              </div>
              <span className="text-[10px] mt-0.5 font-mono tracking-tight">{item.label}</span>
              {isActive && (
                <span className="w-1 h-1 rounded-full bg-cyan-400 mt-0.5 shadow-[0_0_4px_#00f0ff]" />
              )}
            </button>
          );
        })}
      </div>
    </nav>
  );
};
