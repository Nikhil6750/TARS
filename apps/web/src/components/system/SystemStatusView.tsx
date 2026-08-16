import React, { useState } from 'react';
import {
  Cpu,
  Server,
  RefreshCw,
  AlertTriangle
} from 'lucide-react';
import { ConnectionState } from '../../types/companion';
import { detectPlatform } from '../../services/tauri';

interface SystemStatusViewProps {
  connectionState: ConnectionState;
  onUpdateEndpoint: (url: string) => void;
  onReconnect: () => void;
  protocolErrors: Array<{ title: string; errors: string[] }>;
  onClearErrors: () => void;
}

export const SystemStatusView: React.FC<SystemStatusViewProps> = ({
  connectionState,
  onUpdateEndpoint,
  onReconnect,
  protocolErrors,
  onClearErrors
}) => {
  const [endpointInput, setEndpointInput] = useState(connectionState.url);
  const platform = detectPlatform();

  const handleApplyEndpoint = (e: React.FormEvent) => {
    e.preventDefault();
    if (endpointInput.trim()) {
      onUpdateEndpoint(endpointInput.trim());
    }
  };

  const setPresetEndpoint = (url: string) => {
    setEndpointInput(url);
    onUpdateEndpoint(url);
  };

  return (
    <div className="w-full h-full flex flex-col gap-4 p-3 md:p-6 overflow-y-auto max-w-5xl mx-auto">
      {/* Header */}
      <div className="pb-3 border-b border-cyan-500/20">
        <h1 className="text-base font-display-title font-bold text-slate-100 flex items-center gap-2">
          <Cpu className="w-4 h-4 text-cyan-400" />
          SYSTEM & CONNECTIVITY DIAGNOSTICS
        </h1>
        <p className="text-[11px] font-mono text-slate-400">
          Real-time WebSocket transport health, latency, Tailscale endpoint configuration, and runtime environment.
        </p>
      </div>

      {/* Connection Endpoint Configuration */}
      <div className="glass-panel p-5 bg-[#081222]/90">
        <div className="flex items-center justify-between pb-3 border-b border-slate-800">
          <span className="text-xs font-mono font-bold text-slate-200 flex items-center gap-2">
            <Server className="w-4 h-4 text-cyan-400" />
            BACKEND WEBSOCKET ENDPOINT
          </span>
          <span className="text-[10px] font-mono text-slate-400">
            No hardcoded IPs • Tailscale Serve / LAN / Localhost
          </span>
        </div>

        <form onSubmit={handleApplyEndpoint} className="mt-4 flex flex-col sm:flex-row gap-2">
          <input
            type="text"
            value={endpointInput}
            onChange={(e) => setEndpointInput(e.target.value)}
            placeholder="ws://localhost:8000/ws/events or wss://tars-node.ts.net/ws/events"
            className="flex-1 bg-[#040810] border border-slate-700 rounded-lg px-3 py-2 text-xs font-mono text-cyan-300 outline-none focus:border-cyan-500"
          />
          <div className="flex gap-2">
            <button
              type="submit"
              className="px-4 py-2 bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-bold text-xs font-mono rounded-lg transition-colors cursor-pointer"
            >
              Apply
            </button>
            <button
              type="button"
              onClick={onReconnect}
              className="px-3 py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-mono rounded-lg transition-colors flex items-center gap-1.5 cursor-pointer"
            >
              <RefreshCw className="w-3.5 h-3.5" />
              <span>Reconnect</span>
            </button>
          </div>
        </form>

        {/* Presets */}
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span className="text-[10px] font-mono text-slate-500">Quick Presets:</span>
          <button
            type="button"
            onClick={() => setPresetEndpoint('ws://127.0.0.1:8000/ws/events')}
            className="px-2 py-1 rounded bg-slate-900 hover:bg-slate-800 text-[10px] font-mono text-slate-300 border border-slate-800"
          >
            Localhost (127.0.0.1:8000)
          </button>
          <button
            type="button"
            onClick={() => setPresetEndpoint('wss://tars.tailscale.net/ws/events')}
            className="px-2 py-1 rounded bg-slate-900 hover:bg-slate-800 text-[10px] font-mono text-slate-300 border border-slate-800"
          >
            Tailscale Serve (Private)
          </button>
          <button
            type="button"
            onClick={() => setPresetEndpoint('ws://192.168.1.150:8000/ws/events')}
            className="px-2 py-1 rounded bg-slate-900 hover:bg-slate-800 text-[10px] font-mono text-slate-300 border border-slate-800"
          >
            LAN Development IP
          </button>
        </div>
      </div>

      {/* Diagnostics Grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-3 text-xs font-mono">
        <div className="glass-panel p-4 bg-[#081224]/80">
          <span className="text-[10px] text-slate-500 block">CONNECTION STATE</span>
          <span className="text-sm font-bold text-cyan-300 uppercase">{connectionState.status}</span>
          <span className="text-[10px] text-slate-400 block mt-1">
            Attempts: {connectionState.reconnectAttempts}
          </span>
        </div>

        <div className="glass-panel p-4 bg-[#081224]/80">
          <span className="text-[10px] text-slate-500 block">ROUND-TRIP LATENCY</span>
          <span className="text-sm font-bold text-emerald-400">
            {connectionState.latencyMs !== undefined ? `${connectionState.latencyMs} ms` : '—'}
          </span>
          <span className="text-[10px] text-slate-400 block mt-1">WebSocket Ping/Pong</span>
        </div>

        <div className="glass-panel p-4 bg-[#081224]/80">
          <span className="text-[10px] text-slate-500 block">PLATFORM RUNTIME</span>
          <span className="text-sm font-bold text-slate-200 capitalize">{platform.platform}</span>
          <span className="text-[10px] text-cyan-400/80 block mt-1">
            {platform.isNative ? 'Tauri 2 Native Desktop' : 'Responsive PWA / Web'}
          </span>
        </div>

        <div className="glass-panel p-4 bg-[#081224]/80">
          <span className="text-[10px] text-slate-500 block">CONTRACT PROTOCOL</span>
          <span className="text-sm font-bold text-slate-200">v1.0.0</span>
          <span className="text-[10px] text-emerald-400 block mt-1">Strict Validation Active</span>
        </div>
      </div>

      {/* Protocol Errors / Mismatch Log */}
      {protocolErrors.length > 0 && (
        <div className="glass-panel p-4 bg-ruby-950/20 border-ruby-500/40">
          <div className="flex items-center justify-between pb-2 border-b border-ruby-900/60">
            <span className="text-xs font-mono font-bold text-ruby-300 flex items-center gap-2">
              <AlertTriangle className="w-4 h-4 text-ruby-400" />
              CONTRACT VIOLATIONS / PROTOCOL ERRORS
            </span>
            <button
              onClick={onClearErrors}
              className="text-[10px] font-mono text-slate-400 hover:text-slate-200"
            >
              Clear Log
            </button>
          </div>
          <div className="mt-2 space-y-2 max-h-40 overflow-y-auto">
            {protocolErrors.map((err, idx) => (
              <div key={idx} className="p-2 rounded bg-[#040810] border border-ruby-500/30 text-xs font-mono">
                <div className="font-bold text-ruby-300">{err.title}</div>
                <ul className="list-disc list-inside text-[11px] text-slate-300 mt-1 space-y-0.5">
                  {err.errors.map((e, eIdx) => (
                    <li key={eIdx}>{e}</li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};
