import React, { useState, useEffect } from 'react';
import {
  Database,
  Search,
  Shield,
  FileText
} from 'lucide-react';
import { MemoryItem } from '../../types/companion';

const DEMO_FIXTURE_MEMORY_ITEMS: MemoryItem[] = [
  {
    id: 'demo_mem_1',
    type: 'research_knowledge',
    title: '[DEMO/MOCK] H4 Orderblock & FVG Confluence Rules',
    content: 'Gold (XAUUSD) liquidity sweeps on higher timeframe (H4) demand zones require M15 fair value gap confirmation before considering setup valid. Avoid entries during high-impact US CPI releases.',
    source: 'Obsidian Vault: Trading/Strategies/OrderBlock_V2.md',
    timestamp: '2026-08-15T18:00:00Z',
    tags: ['XAUUSD', 'Orderblock', 'FVG', 'Risk', 'DEMO']
  },
  {
    id: 'demo_mem_2',
    type: 'operational_state',
    title: '[DEMO/MOCK] Live Session Risk Allocation Limits',
    content: 'Max combined index risk (ES + NQ) capped at 2.25% of account balance. Single instrument max risk 1.0%. All warnings trigger alert state in companion UI.',
    source: 'SQLite: operational_state / risk_parameters',
    timestamp: '2026-08-16T08:00:00Z',
    tags: ['Risk', 'Limits', 'Portfolio', 'DEMO']
  },
  {
    id: 'demo_mem_3',
    type: 'research_knowledge',
    title: '[DEMO/MOCK] Strategy Confluence Checklist — Trend Continuation',
    content: 'Requires breakout retest confluence and structural market maker buy model confirmation. Deterministic risk parameters only.',
    source: 'Obsidian Vault / strategy_notes.md',
    timestamp: '2026-08-14T12:00:00Z',
    tags: ['Research', 'Confluence', 'Strategy', 'DEMO']
  }
];

interface MemoryViewProps {
  apiEndpoint?: string;
  mockModeActive?: boolean;
}

export const MemoryView: React.FC<MemoryViewProps> = ({
  apiEndpoint = 'http://127.0.0.1:8000',
  mockModeActive = false
}) => {
  const [search, setSearch] = useState('');
  const [activeCategory, setActiveCategory] = useState<string>('ALL');
  const [backendItems, setBackendItems] = useState<MemoryItem[]>([]);
  const [hasSearched, setHasSearched] = useState(false);

  useEffect(() => {
    if (!search.trim()) {
      setBackendItems([]);
      setHasSearched(false);
      return;
    }
    setHasSearched(true);
    const timer = setTimeout(async () => {
      try {
        const sourceParam =
          activeCategory === 'operational_state'
            ? '&source=conversation'
            : activeCategory === 'research_knowledge'
            ? '&source=vault'
            : '';
        const res = await fetch(
          `${apiEndpoint}/api/v1/memory/search?q=${encodeURIComponent(search.trim())}${sourceParam}`
        );
        if (res.ok) {
          const results = await res.json();
          if (Array.isArray(results) && results.length > 0) {
            const mapped: MemoryItem[] = results.map((r: { source?: string; path?: string; snippet?: string; content?: string }, idx: number) => ({
              id: `be_mem_${idx}`,
              type: r.source === 'vault' ? 'research_knowledge' : 'operational_state',
              title: r.source === 'vault' ? `Obsidian: ${r.path || 'Vault Note'}` : 'Conversation Memory',
              content: r.snippet || r.content || '',
              source: r.source === 'vault' ? `Vault / ${r.path || ''}` : 'SQLite / conversation_history',
              timestamp: new Date().toISOString(),
              tags: ['Retrieved', r.source || 'memory']
            }));
            setBackendItems(mapped);
            return;
          }
        }
      } catch (err) {
        console.warn('[TARS Memory Search] Query error:', err);
      }
      setBackendItems([]);
    }, 300);

    return () => clearTimeout(timer);
  }, [search, activeCategory, apiEndpoint]);

  // In real mode, NEVER show demo items as real. Demo items only appear when mock mode is explicitly enabled.
  const itemsToDisplay = backendItems.length > 0
    ? backendItems
    : mockModeActive
    ? DEMO_FIXTURE_MEMORY_ITEMS
    : [];

  const filteredItems = itemsToDisplay.filter((item) => {
    if (activeCategory !== 'ALL' && item.type !== activeCategory) return false;
    if (search && backendItems.length === 0 && mockModeActive) {
      const q = search.toLowerCase();
      return (
        item.title.toLowerCase().includes(q) ||
        item.content.toLowerCase().includes(q) ||
        item.tags.some((t) => t.toLowerCase().includes(q))
      );
    }
    return true;
  });

  return (
    <div className="w-full h-full flex flex-col gap-4 p-3 md:p-6 overflow-y-auto max-w-7xl mx-auto">
      {/* Header */}
      <div className="pb-3 border-b border-cyan-500/20 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
        <div>
          <h1 className="text-base font-display-title font-bold text-slate-100 flex items-center gap-2">
            <Database className="w-4 h-4 text-cyan-400" />
            MEMORY & RESEARCH ACCESS SHELL
          </h1>
          <p className="text-[11px] font-mono text-slate-400">
            Unified SQLite FTS5 search & Obsidian research notes layer.
          </p>
        </div>

        {/* Search */}
        <div className="relative w-full sm:w-64">
          <Search className="w-3.5 h-3.5 text-slate-500 absolute left-2.5 top-2.5" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search notes & memory..."
            className="w-full bg-[#08101e] border border-slate-700/80 rounded-lg pl-8 pr-2.5 py-1.5 text-xs font-mono text-slate-200 outline-none focus:border-cyan-500"
          />
        </div>
      </div>

      {/* ADR-013 Boundary Discipline Alert */}
      <div className="p-3 rounded-xl bg-[#0a1628] border border-cyan-500/30 text-xs font-mono flex items-start gap-3">
        <Shield className="w-4 h-4 text-cyan-400 shrink-0 mt-0.5" />
        <div className="text-[11px] leading-relaxed text-slate-300">
          <span className="font-bold text-cyan-300">Architectural Boundary Enforcement (ADR-013): </span>
          Memory text, conversation turns, and free-text notes are never used as proof of trading performance. Validated trading intelligence is originated exclusively by <code className="text-cyan-400">quant_brain</code>.
        </div>
      </div>

      {/* 4 Boundary Categorization Badges */}
      <div className="flex flex-wrap gap-2 text-xs font-mono">
        <button
          onClick={() => setActiveCategory('ALL')}
          className={`px-3 py-1 rounded-md transition-colors cursor-pointer ${
            activeCategory === 'ALL'
              ? 'bg-cyan-500/30 text-cyan-200 border border-cyan-500/40'
              : 'bg-[#091220] text-slate-400 hover:text-slate-200'
          }`}
        >
          All Layers
        </button>
        <button
          onClick={() => setActiveCategory('operational_state')}
          className={`px-3 py-1 rounded-md transition-colors cursor-pointer ${
            activeCategory === 'operational_state'
              ? 'bg-cyan-500/30 text-cyan-200 border border-cyan-500/40'
              : 'bg-[#091220] text-slate-400 hover:text-slate-200'
          }`}
        >
          Operational State (SQLite)
        </button>
        <button
          onClick={() => setActiveCategory('research_knowledge')}
          className={`px-3 py-1 rounded-md transition-colors cursor-pointer ${
            activeCategory === 'research_knowledge'
              ? 'bg-cyan-500/30 text-cyan-200 border border-cyan-500/40'
              : 'bg-[#091220] text-slate-400 hover:text-slate-200'
          }`}
        >
          Research Knowledge (Obsidian)
        </button>
      </div>

      {/* Memory Results */}
      {filteredItems.length === 0 ? (
        <div className="glass-panel p-12 text-center flex flex-col items-center justify-center bg-[#081224]/50">
          <FileText className="w-8 h-8 text-slate-600 mb-2" />
          <p className="text-sm font-mono text-slate-400">
            {hasSearched
              ? 'No research notes or memory items matched your query.'
              : 'Enter a search query above to query SQLite operational state and Obsidian vault notes.'}
          </p>
          {!mockModeActive && (
            <p className="text-[11px] font-mono text-slate-500 mt-1">
              Real Backend Mode active. Grounded records only.
            </p>
          )}
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {filteredItems.map((item) => (
            <div
              key={item.id}
              className="glass-panel p-4 flex flex-col justify-between bg-[#081224]/90 hover:border-cyan-500/40 transition-all"
            >
              <div>
                <div className="flex items-center justify-between pb-2 border-b border-slate-800">
                  <span className="text-xs font-mono font-bold text-slate-200">{item.title}</span>
                  <span className="px-2 py-0.5 rounded text-[9px] font-mono uppercase bg-cyan-950/80 border border-cyan-500/30 text-cyan-300">
                    {item.type.replace('_', ' ')}
                  </span>
                </div>

                <p className="my-3 text-xs leading-relaxed text-slate-300 font-sans">
                  {item.content}
                </p>

                <div className="flex flex-wrap gap-1 mt-2">
                  {item.tags.map((tag) => (
                    <span
                      key={tag}
                      className={`px-1.5 py-0.5 rounded border text-[10px] font-mono ${
                        tag === 'DEMO'
                          ? 'bg-amber-950/60 border-amber-500/40 text-amber-300'
                          : 'bg-slate-900 border-slate-800 text-slate-400'
                      }`}
                    >
                      #{tag}
                    </span>
                  ))}
                </div>
              </div>

              <div className="mt-4 pt-2 border-t border-slate-800 flex items-center justify-between text-[10px] font-mono text-slate-500">
                <span className="truncate max-w-[200px]">{item.source}</span>
                <span>{new Date(item.timestamp).toLocaleDateString()}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};
