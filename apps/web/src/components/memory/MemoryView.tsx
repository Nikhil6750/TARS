import React, { useState } from 'react';
import {
  Database,
  Search,
  Shield
} from 'lucide-react';
import { MemoryItem } from '../../types/companion';

const SAMPLE_MEMORY_ITEMS: MemoryItem[] = [
  {
    id: 'mem_1',
    type: 'research_knowledge',
    title: 'H4 Orderblock & FVG Confluence Rules',
    content: 'Gold (XAUUSD) liquidity sweeps on higher timeframe (H4) demand zones require M15 fair value gap confirmation before considering setup valid. Avoid entries during high-impact US CPI releases.',
    source: 'Obsidian Vault: Trading/Strategies/OrderBlock_V2.md',
    timestamp: '2026-08-15T18:00:00Z',
    tags: ['XAUUSD', 'Orderblock', 'FVG', 'Risk']
  },
  {
    id: 'mem_2',
    type: 'operational_state',
    title: 'Live Session Risk Allocation Limits',
    content: 'Max combined index risk (ES + NQ) capped at 2.25% of account balance. Single instrument max risk 1.0%. All warnings trigger alert state in companion UI.',
    source: 'SQLite: operational_state / risk_parameters',
    timestamp: '2026-08-16T08:00:00Z',
    tags: ['Risk', 'Limits', 'Portfolio']
  },
  {
    id: 'mem_3',
    type: 'journal_reference',
    title: 'quant_brain Strategy Backtest #402 — Trend Continuation',
    content: 'Verified DSR > 1.8 across 5-year walk-forward test on NQ. Realized Sharpe 2.12. (Referenced from quant_brain core database).',
    source: 'quant_brain / strategy_registry',
    timestamp: '2026-08-14T12:00:00Z',
    tags: ['quant_brain', 'DSR', 'NQ', 'Strategy']
  }
];

export const MemoryView: React.FC = () => {
  const [search, setSearch] = useState('');
  const [activeCategory, setActiveCategory] = useState<string>('ALL');

  const filteredItems = SAMPLE_MEMORY_ITEMS.filter((item) => {
    if (activeCategory !== 'ALL' && item.type !== activeCategory) return false;
    if (search) {
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
        <button
          onClick={() => setActiveCategory('journal_reference')}
          className={`px-3 py-1 rounded-md transition-colors cursor-pointer ${
            activeCategory === 'journal_reference'
              ? 'bg-cyan-500/30 text-cyan-200 border border-cyan-500/40'
              : 'bg-[#091220] text-slate-400 hover:text-slate-200'
          }`}
        >
          quant_brain Reference
        </button>
      </div>

      {/* Memory Results */}
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
                    className="px-1.5 py-0.5 rounded bg-slate-900 border border-slate-800 text-[10px] font-mono text-slate-400"
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
    </div>
  );
};
