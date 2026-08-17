import React, { useState } from 'react';
import {
  Globe,
  ChevronLeft,
  ChevronRight,
  ArrowDown,
  Layers,
  FileText,
  X,
} from 'lucide-react';
import { BrowserPageContext, DOMElementSummary } from '../../types/actions';
import { browserControlService } from '../../services/browser-control';

interface BrowserContextCardProps {
  context: BrowserPageContext;
  onNavigate: (url: string) => Promise<void> | void;
  onClose?: () => void;
}

export const BrowserContextCard: React.FC<BrowserContextCardProps> = ({
  context,
  onNavigate,
  onClose,
}) => {
  const [urlInput, setUrlInput] = useState(context.url);
  const [activeTab, setActiveTab] = useState<'dom' | 'summary' | 'tabs'>('dom');
  const [summaryText, setSummaryText] = useState<string | null>(null);

  const handleUrlSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (urlInput.trim()) {
      onNavigate(urlInput.trim());
    }
  };

  const handleReadSummary = () => {
    const text = browserControlService.readPageText('summary');
    setSummaryText(text);
    setActiveTab('summary');
  };

  const handleScroll = (deltaY: number) => {
    browserControlService.scroll(deltaY);
  };

  return (
    <div className="bg-[#070e1b] border border-cyan-500/30 rounded-xl p-3 shadow-[0_0_20px_rgba(6,182,212,0.12)] font-mono text-xs select-none animate-in fade-in duration-150">
      {/* Header with URL Bar */}
      <div className="flex items-center justify-between pb-2 border-b border-slate-800">
        <div className="flex items-center gap-1.5 min-w-0 flex-1 mr-2">
          <Globe className="w-3.5 h-3.5 text-blue-400 shrink-0" />
          <form onSubmit={handleUrlSubmit} className="flex-1 flex items-center min-w-0">
            <input
              type="text"
              value={urlInput}
              onChange={(e) => setUrlInput(e.target.value)}
              className="w-full bg-[#03060a] border border-slate-800 focus:border-cyan-500/40 rounded px-2 py-0.5 text-[11px] text-cyan-300 focus:outline-none truncate"
              placeholder="https://..."
            />
          </form>
        </div>

        <div className="flex items-center gap-1 shrink-0">
          <button
            onClick={() => browserControlService.back()}
            disabled={!context.can_go_back}
            className="p-1 text-slate-400 hover:text-cyan-300 transition-colors disabled:opacity-30"
            title="Back"
          >
            <ChevronLeft className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={() => browserControlService.forward()}
            disabled={!context.can_go_forward}
            className="p-1 text-slate-400 hover:text-cyan-300 transition-colors disabled:opacity-30"
            title="Forward"
          >
            <ChevronRight className="w-3.5 h-3.5" />
          </button>
          {onClose && (
            <button
              onClick={onClose}
              className="p-1 text-slate-500 hover:text-slate-200 transition-colors"
              title="Close Browser Card"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          )}
        </div>
      </div>

      {/* Quick Action Tools */}
      <div className="flex items-center gap-1.5 my-2 border-b border-slate-800/80 pb-1.5 text-[10px]">
        <button
          onClick={() => setActiveTab('dom')}
          className={`px-2 py-0.5 rounded transition-colors flex items-center gap-1 ${
            activeTab === 'dom'
              ? 'bg-cyan-950/90 text-cyan-300 border border-cyan-500/40 font-bold'
              : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          <Layers className="w-3 h-3" />
          <span>DOM ({context.dom_tree?.length || 0})</span>
        </button>

        <button
          onClick={handleReadSummary}
          className={`px-2 py-0.5 rounded transition-colors flex items-center gap-1 ${
            activeTab === 'summary'
              ? 'bg-cyan-950/90 text-cyan-300 border border-cyan-500/40 font-bold'
              : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          <FileText className="w-3 h-3" />
          <span>Read Text</span>
        </button>

        <button
          onClick={() => handleScroll(400)}
          className="px-2 py-0.5 rounded bg-black/40 hover:bg-slate-800 text-slate-400 hover:text-cyan-300 border border-slate-800 transition-colors flex items-center gap-1"
          title="Scroll Down"
        >
          <ArrowDown className="w-3 h-3" />
          <span>Scroll</span>
        </button>
      </div>

      {/* Content */}
      {activeTab === 'dom' && (
        <div className="max-h-36 overflow-y-auto space-y-1 custom-scrollbar pr-1">
          {context.dom_tree && context.dom_tree.length > 0 ? (
            context.dom_tree.slice(0, 25).map((el: DOMElementSummary, idx: number) => (
              <div
                key={idx}
                className="flex items-center justify-between p-1 rounded bg-black/40 border border-slate-800/80 text-[10px]"
              >
                <div className="flex items-center gap-1.5 truncate">
                  <span className="px-1 py-0.2 rounded bg-blue-950/60 border border-blue-500/30 text-blue-300 text-[9px]">
                    {el.tag}
                  </span>
                  <span className="text-slate-200 truncate">{el.text || el.placeholder || el.selector}</span>
                </div>
                {el.is_sensitive && (
                  <span className="px-1 py-0.2 rounded bg-amber-950 text-amber-300 text-[9px] border border-amber-500/30 shrink-0">
                    SENSITIVE
                  </span>
                )}
              </div>
            ))
          ) : (
            <div className="p-2 text-center text-slate-500 text-[11px]">
              No DOM elements inspected on page.
            </div>
          )}
        </div>
      )}

      {activeTab === 'summary' && (
        <div className="max-h-36 overflow-y-auto p-2 bg-black/40 rounded border border-slate-800 text-[10px] text-slate-300 whitespace-pre-wrap custom-scrollbar">
          {summaryText || 'No text extracted.'}
        </div>
      )}
    </div>
  );
};
