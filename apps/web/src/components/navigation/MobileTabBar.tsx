import React from 'react';
import { Bot, LayoutGrid, Settings } from 'lucide-react';
import { ActiveTab } from '../../types/companion';

interface MobileTabBarProps {
  activeTab: ActiveTab;
  setActiveTab: (tab: ActiveTab) => void;
}

export const MobileTabBar: React.FC<MobileTabBarProps> = ({
  activeTab,
  setActiveTab,
}) => {
  const tabs: Array<{ tab: ActiveTab; label: string; icon: React.ComponentType<{ className?: string }> }> = [
    { tab: 'tars', label: 'TARS', icon: Bot },
    { tab: 'workspace', label: 'Workspace', icon: LayoutGrid },
    { tab: 'settings', label: 'Settings', icon: Settings },
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
