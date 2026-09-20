import React from 'react';
import {
  MessageSquare,
  Plus,
  Trash2,
} from 'lucide-react';

export interface ChatSessionMeta {
  id: string;
  title: string;
  createdAt: string;
  messageCount: number;
}

interface SidebarProps {
  sessions: ChatSessionMeta[];
  activeSessionId: string;
  onSelectSession: (id: string) => void;
  onNewChat: () => void;
  onClearHistory?: () => void;
  isOpen?: boolean;
}

function formatSessionDate(dateStr: string): string {
  try {
    const d = new Date(dateStr);
    const now = new Date();
    const isToday = d.toDateString() === now.toDateString();
    if (isToday) {
      return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    }
    const yesterday = new Date(now);
    yesterday.setDate(yesterday.getDate() - 1);
    if (d.toDateString() === yesterday.toDateString()) {
      return 'Yesterday';
    }
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  } catch {
    return '';
  }
}

export const Sidebar: React.FC<SidebarProps> = ({
  sessions,
  activeSessionId,
  onSelectSession,
  onNewChat,
  onClearHistory,
  isOpen = true,
}) => {
  if (!isOpen) return null;

  return (
    <aside className="w-64 bg-[#f7f7f8] border-r border-[#e5e7eb] flex flex-col justify-between p-3.5 select-none shrink-0 h-full">
      {/* Top Section */}
      <div className="flex flex-col min-h-0 flex-1">
        {/* + New Chat Button Matching Reference */}
        <button
          type="button"
          onClick={onNewChat}
          className="flex items-center justify-between px-3.5 py-2.5 rounded-xl bg-white hover:bg-[#f0f0f1] border border-[#e5e7eb] text-[#1f2937] hover:text-[#111827] transition-all cursor-pointer shadow-xs mb-5 group"
        >
          <div className="flex items-center gap-2 font-medium text-[13px]">
            <Plus className="w-4 h-4 text-[#4b5563] group-hover:text-[#111827] stroke-[2.2]" />
            <span>New Chat</span>
          </div>
          <span className="text-[11px] text-[#9ca3af] font-mono bg-[#f3f4f6] px-1.5 py-0.5 rounded border border-[#e5e7eb]">
            Ctrl + N
          </span>
        </button>

        {/* Section Header */}
        <div className="px-1.5 mb-2.5 text-xs font-medium text-[#6b7280] font-sans">
          Recent
        </div>

        {/* Session List with Comfortable Row Heights */}
        <div className="flex-1 overflow-y-auto custom-scrollbar space-y-1 pr-0.5">
          {sessions.length === 0 ? (
            <div className="px-2 py-8 text-center text-xs text-[#9ca3af] font-normal italic">
              No conversations
            </div>
          ) : (
            sessions.map((sess) => {
              const isActive = sess.id === activeSessionId;
              const formattedDate = formatSessionDate(sess.createdAt);

              return (
                <button
                  key={sess.id}
                  type="button"
                  onClick={() => onSelectSession(sess.id)}
                  className={`w-full min-h-[42px] flex items-center justify-between px-3 py-2.5 rounded-lg text-[13px] transition-all cursor-pointer text-left ${
                    isActive
                      ? 'bg-[#e5e7eb]/80 text-[#111827] font-medium'
                      : 'text-[#4b5563] hover:text-[#111827] hover:bg-[#ececec]/60 font-normal'
                  }`}
                >
                  <div className="flex items-center gap-2.5 min-w-0 flex-1 mr-2">
                    <MessageSquare className="w-3.5 h-3.5 shrink-0 text-[#9ca3af] stroke-[1.6]" />
                    <span className="truncate">{sess.title}</span>
                  </div>
                  {formattedDate && (
                    <span className="text-[11px] text-[#9ca3af] shrink-0 font-normal">
                      {formattedDate}
                    </span>
                  )}
                </button>
              );
            })
          )}
        </div>
      </div>

      {/* Bottom Action: Clear history Matching Reference */}
      <div className="pt-3 border-t border-[#e5e7eb] shrink-0">
        <button
          type="button"
          onClick={onClearHistory}
          className="w-full h-10 flex items-center gap-2 px-3.5 rounded-xl bg-white hover:bg-[#f0f0f1] border border-[#e5e7eb] text-[#6b7280] hover:text-[#1f2937] text-xs font-normal transition-all cursor-pointer shadow-xs"
        >
          <Trash2 className="w-3.5 h-3.5 text-[#9ca3af] stroke-[1.8]" />
          <span>Clear history</span>
        </button>
      </div>
    </aside>
  );
};
