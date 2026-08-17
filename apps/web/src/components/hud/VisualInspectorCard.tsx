import React, { useState } from 'react';
import { Eye, ShieldAlert, Layers, Camera, RefreshCw, X, Maximize } from 'lucide-react';
import { ScreenCaptureResult, UIElementNode, MonitorInfo } from '../../types/actions';

interface VisualInspectorCardProps {
  capture: ScreenCaptureResult | null;
  uiTree: UIElementNode | null;
  monitors: MonitorInfo[];
  onRefreshCapture: () => Promise<void> | void;
  onClose?: () => void;
  isLoading?: boolean;
}

export const VisualInspectorCard: React.FC<VisualInspectorCardProps> = ({
  capture,
  uiTree,
  monitors,
  onRefreshCapture,
  onClose,
  isLoading = false,
}) => {
  const [activeTab, setActiveTab] = useState<'visual' | 'elements' | 'monitors'>('visual');

  return (
    <div className="bg-[#070e1b] border border-cyan-500/30 rounded-xl p-3 shadow-[0_0_20px_rgba(6,182,212,0.12)] font-mono text-xs select-none animate-in fade-in duration-150">
      {/* Header */}
      <div className="flex items-center justify-between pb-2 border-b border-slate-800">
        <div className="flex items-center gap-1.5 text-cyan-400 font-bold tracking-wider text-[11px]">
          <Eye className="w-3.5 h-3.5" />
          <span>WHAT TARS SEES</span>
          <span className="text-[9px] px-1 py-0.2 rounded bg-cyan-950/80 border border-cyan-500/30 text-cyan-300">
            DPI: {capture?.dpi || 96} ({Math.round((capture?.scale_factor || 1.0) * 100)}%)
          </span>
        </div>

        <div className="flex items-center gap-1">
          <button
            onClick={onRefreshCapture}
            disabled={isLoading}
            className="p-1 text-slate-400 hover:text-cyan-300 transition-colors disabled:opacity-50"
            title="Refresh Visual Snapshot"
          >
            <RefreshCw className={`w-3 h-3 ${isLoading ? 'animate-spin text-cyan-400' : ''}`} />
          </button>
          {onClose && (
            <button
              onClick={onClose}
              className="p-1 text-slate-500 hover:text-slate-200 transition-colors"
              title="Close Inspector"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          )}
        </div>
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-1 my-2 border-b border-slate-800/80 pb-1 text-[10px]">
        <button
          onClick={() => setActiveTab('visual')}
          className={`px-2 py-0.5 rounded transition-colors flex items-center gap-1 ${
            activeTab === 'visual'
              ? 'bg-cyan-950/90 text-cyan-300 border border-cyan-500/40 font-bold'
              : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          <Camera className="w-3 h-3" />
          <span>Snapshot</span>
        </button>

        <button
          onClick={() => setActiveTab('elements')}
          className={`px-2 py-0.5 rounded transition-colors flex items-center gap-1 ${
            activeTab === 'elements'
              ? 'bg-cyan-950/90 text-cyan-300 border border-cyan-500/40 font-bold'
              : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          <Layers className="w-3 h-3" />
          <span>UI Tree ({uiTree?.children.length || 0})</span>
        </button>

        <button
          onClick={() => setActiveTab('monitors')}
          className={`px-2 py-0.5 rounded transition-colors flex items-center gap-1 ${
            activeTab === 'monitors'
              ? 'bg-cyan-950/90 text-cyan-300 border border-cyan-500/40 font-bold'
              : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          <Maximize className="w-3 h-3" />
          <span>Monitors ({monitors.length})</span>
        </button>
      </div>

      {/* Content based on tab */}
      {activeTab === 'visual' && (
        <div className="space-y-2">
          {capture?.is_secure_desktop ? (
            <div className="p-2.5 bg-rose-950/60 border border-rose-500/40 rounded flex items-center gap-2 text-rose-300 text-[11px]">
              <ShieldAlert className="w-4 h-4 text-rose-400 shrink-0" />
              <div>
                <span className="font-bold block">SECURE DESKTOP ACTIVE</span>
                <span className="text-[10px] text-slate-300">Screen capture prohibited for system security (UAC / Lock Screen).</span>
              </div>
            </div>
          ) : capture?.image_data_base64 ? (
            <div className="relative rounded-lg overflow-hidden border border-slate-700 bg-black/60 aspect-video flex items-center justify-center">
              <img
                src={capture.image_data_base64}
                alt="Active Window Snapshot"
                className="w-full h-full object-contain"
              />
              <div className="absolute bottom-1 right-1 px-1.5 py-0.5 bg-black/80 rounded text-[9px] text-slate-400">
                {capture.width}x{capture.height}px
              </div>
            </div>
          ) : (
            <div className="p-3 bg-black/40 border border-slate-800 rounded text-center text-slate-500 text-[11px]">
              No active snapshot captured yet.
            </div>
          )}

          {capture && (
            <div className="grid grid-cols-2 gap-1 text-[10px] text-slate-400 bg-black/30 p-1.5 rounded border border-slate-800/80">
              <div><span className="text-slate-500">Source:</span> {capture.source}</div>
              <div><span className="text-slate-500">Window:</span> {capture.executable}</div>
              <div><span className="text-slate-500">Bounds:</span> {capture.bounds.width}x{capture.bounds.height}</div>
              <div><span className="text-slate-500">Time:</span> {new Date(capture.captured_at).toLocaleTimeString()}</div>
            </div>
          )}
        </div>
      )}

      {activeTab === 'elements' && (
        <div className="max-h-36 overflow-y-auto space-y-1 custom-scrollbar pr-1">
          {uiTree && uiTree.children.length > 0 ? (
            uiTree.children.map((child, idx) => (
              <div
                key={idx}
                className="flex items-center justify-between p-1 rounded bg-black/40 border border-slate-800/80 text-[10px]"
              >
                <div className="flex items-center gap-1.5 truncate">
                  <span className="px-1 py-0.2 rounded bg-cyan-950/60 border border-cyan-500/30 text-cyan-300 text-[9px]">
                    {child.role}
                  </span>
                  <span className="text-slate-200 truncate">{child.name || child.class_name}</span>
                </div>
                {child.bounds && (
                  <span className="text-slate-500 text-[9px] shrink-0">
                    {child.bounds.width}x{child.bounds.height}
                  </span>
                )}
              </div>
            ))
          ) : (
            <div className="p-2 text-center text-slate-500 text-[11px]">
              No native UI elements enumerated.
            </div>
          )}
        </div>
      )}

      {activeTab === 'monitors' && (
        <div className="space-y-1">
          {monitors.map((m, idx) => (
            <div
              key={idx}
              className="p-1.5 rounded bg-black/40 border border-slate-800/80 text-[10px] flex items-center justify-between"
            >
              <div>
                <span className="font-semibold text-slate-200">{m.name}</span>
                {m.is_primary && (
                  <span className="ml-1 px-1 py-0.2 rounded bg-emerald-950/80 text-emerald-300 border border-emerald-500/30 text-[9px]">
                    PRIMARY
                  </span>
                )}
              </div>
              <span className="text-cyan-300 text-[9px]">
                {m.bounds.width}x{m.bounds.height} ({m.scale_factor * 100}% DPI)
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};
