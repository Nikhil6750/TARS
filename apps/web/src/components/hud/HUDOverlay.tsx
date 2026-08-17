import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  Maximize2,
  Minimize2,
  Mic,
  Send,
  Zap,
  Terminal,
  BookOpen,
  AppWindow,
  Globe,
  AlertTriangle,
} from 'lucide-react';
import { TARSCharacter } from '../character/TARSCharacter';
import { CompanionVisualState } from '../../types/companion';
import { TARSTradingEvent } from '../../types/trading-event';
import {
  ActionRequest,
  ActionResult,
  ActiveWindowContext,
} from '../../types/actions';
import { actionRuntimeClient } from '../../services/actions';
import { nativeBridge } from '../../services/native-bridge';
import { ActiveContextBar } from './ActiveContextBar';
import { ActionConfirmationCard } from './ActionConfirmationCard';
import { ActionResultView } from './ActionResultView';

interface HUDOverlayProps {
  companionState: CompanionVisualState;
  onExpand: () => void;
  activeSetups: TARSTradingEvent[];
  criticalWarnings: string[];
  isListening: boolean;
  onTogglePushToTalk: () => void;
  audioVolume: number;
  apiEndpoint?: string;
  onSendMessage?: (text: string) => void;
}

export const HUDOverlay: React.FC<HUDOverlayProps> = ({
  companionState,
  onExpand,
  activeSetups,
  criticalWarnings,
  isListening,
  onTogglePushToTalk,
  audioVolume,
  apiEndpoint = 'http://127.0.0.1:8000',
  onSendMessage,
}) => {
  // Active foreground window context
  const [activeContext, setActiveContext] = useState<ActiveWindowContext | null>(null);
  const [isRefreshingContext, setIsRefreshingContext] = useState(false);

  // Command / Prompt Input
  const [inputText, setInputText] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);

  // Action Runtime States
  const [pendingConfirmation, setPendingConfirmation] = useState<ActionRequest | null>(null);
  const [latestResult, setLatestResult] = useState<ActionResult | null>(null);

  const inputRef = useRef<HTMLInputElement>(null);

  // Refresh active window context on mount or summon
  const refreshActiveContext = useCallback(async () => {
    setIsRefreshingContext(true);
    try {
      const ctx = await nativeBridge.getActiveWindowContext();
      if (ctx) {
        setActiveContext(ctx);
      }
    } catch (err) {
      console.warn('[HUDOverlay] Failed to refresh context:', err);
    } finally {
      setIsRefreshingContext(false);
    }
  }, []);

  useEffect(() => {
    refreshActiveContext();
    // Auto-focus input on mount
    inputRef.current?.focus();
  }, [refreshActiveContext]);

  // Subscribe to action results
  useEffect(() => {
    actionRuntimeClient.setEndpoint(apiEndpoint);

    const unsub = actionRuntimeClient.onAnyActionResult((result, request) => {
      setLatestResult(result);
      if (result.status === 'CONFIRMATION_REQUIRED' && request) {
        setPendingConfirmation(request);
      } else if (result.status !== 'CONFIRMATION_REQUIRED' && result.status !== 'PENDING') {
        if (pendingConfirmation && pendingConfirmation.id === result.request_id) {
          setPendingConfirmation(null);
        }
      }
    });

    return () => unsub();
  }, [apiEndpoint, pendingConfirmation]);

  // Check for deterministic command match preview
  const deterministicPreview = inputText.trim()
    ? actionRuntimeClient.parseDeterministicCommand(inputText, activeContext)
    : null;

  // Handle Command Submission
  const handleSubmitCommand = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    const text = inputText.trim();
    if (!text || isSubmitting) return;

    setIsSubmitting(true);
    try {
      // 1. Check if it's a deterministic command (bypasses LLM)
      const deterministicReq = actionRuntimeClient.parseDeterministicCommand(
        text,
        activeContext,
        'hud'
      );

      if (deterministicReq) {
        setInputText('');
        const res = await actionRuntimeClient.submitAction(deterministicReq);
        if (res.status === 'CONFIRMATION_REQUIRED') {
          setPendingConfirmation(deterministicReq);
        }
      } else {
        // 2. Natural language query or prompt
        if (onSendMessage) {
          onSendMessage(text);
          setInputText('');
        } else {
          // If no chat handler, create generic action request
          const req = actionRuntimeClient.createRequest({
            skill: 'windows_app',
            action: 'prompt',
            arguments: { prompt: text },
            source: 'hud',
            activeContext,
          });
          setInputText('');
          await actionRuntimeClient.submitAction(req);
        }
      }
    } catch (err) {
      console.error('[HUDOverlay] Submit command error:', err);
    } finally {
      setIsSubmitting(false);
    }
  };

  // Handle Action Confirmation / Denial
  const handleConfirmAction = async (requestId: string) => {
    try {
      await actionRuntimeClient.respondToConfirmation(requestId, true);
      setPendingConfirmation(null);
    } catch (err) {
      console.error('[HUDOverlay] Confirm error:', err);
    }
  };

  const handleDenyAction = async (requestId: string, reason?: string) => {
    try {
      await actionRuntimeClient.respondToConfirmation(requestId, false, reason);
      setPendingConfirmation(null);
    } catch (err) {
      console.error('[HUDOverlay] Deny error:', err);
    }
  };

  // Quick Skill Helpers
  const handleQuickSkill = (skill: string, action: string, args: Record<string, unknown>) => {
    const req = actionRuntimeClient.createRequest({
      skill,
      action,
      arguments: args,
      source: 'hud',
      activeContext,
    });
    actionRuntimeClient.submitAction(req);
  };

  return (
    <div className="w-full h-full flex flex-col justify-between p-3.5 bg-[#040810]/95 backdrop-blur-md border-2 border-cyan-500/40 rounded-2xl select-none font-sans text-slate-100 shadow-[0_0_35px_rgba(6,182,212,0.15)] overflow-hidden">
      {/* Top HUD Header */}
      <div className="flex items-center justify-between pb-2.5 border-b border-slate-800/90 shrink-0">
        <div className="flex items-center gap-2">
          <span className="relative flex h-2.5 w-2.5">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-cyan-400 opacity-75" />
            <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-cyan-500" />
          </span>
          <div>
            <span className="font-display-title font-bold text-xs tracking-wider text-slate-100">
              TARS HUD
            </span>
            <span className="ml-1.5 px-1.5 py-0.2 rounded text-[9px] font-mono bg-cyan-950/80 text-cyan-300 border border-cyan-500/30">
              WAVE 2A
            </span>
          </div>
        </div>

        <div className="flex items-center gap-1.5">
          <button
            onClick={() => nativeBridge.hideHUD()}
            className="p-1 rounded bg-slate-900 hover:bg-slate-800 text-slate-400 hover:text-slate-200 border border-slate-700/80 transition-colors text-xs"
            title="Minimize to Tray (Keeps Running)"
          >
            <Minimize2 className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={onExpand}
            className="p-1 rounded bg-slate-900 hover:bg-slate-800 text-slate-400 hover:text-cyan-400 border border-slate-700/80 transition-colors text-xs"
            title="Expand to Full Workstation"
          >
            <Maximize2 className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {/* Active Foreground Window Snapshot */}
      <div className="mt-2 shrink-0">
        <ActiveContextBar
          activeContext={activeContext}
          onRefresh={refreshActiveContext}
          isLoading={isRefreshingContext}
        />
      </div>

      {/* Center Interactive Stage */}
      <div className="flex-1 my-2 overflow-y-auto space-y-2 pr-0.5 custom-scrollbar min-h-0 flex flex-col justify-center">
        {/* Pending Action Confirmation Card if required */}
        {pendingConfirmation ? (
          <ActionConfirmationCard
            request={pendingConfirmation}
            onConfirm={handleConfirmAction}
            onDeny={handleDenyAction}
          />
        ) : latestResult ? (
          /* Real Action Result View */
          <ActionResultView
            result={latestResult}
            onDismiss={() => setLatestResult(null)}
          />
        ) : (
          /* Companion Avatar Visualizer */
          <div className="flex flex-col items-center justify-center py-2">
            <TARSCharacter
              state={companionState}
              audioVolume={audioVolume}
              size="compact"
              onClick={onTogglePushToTalk}
            />
            <span className="text-[11px] font-mono text-slate-400 mt-2">
              {companionState === 'LISTENING'
                ? 'LISTENING... (RELEASE TO PROCESS)'
                : companionState === 'THINKING'
                ? 'DECIDING ACTION PERMISSION...'
                : companionState === 'ALERT'
                ? 'MARKET SIGNAL TRIGGERED'
                : 'ALWAYS-AVAILABLE ASSISTANT'}
            </span>
          </div>
        )}
      </div>

      {/* Active Market Confluence or Warning Indicator */}
      {(criticalWarnings.length > 0 || activeSetups.some((s) => s.state === 'SETUP_VALID')) && (
        <div className="mb-1.5 px-2 py-1 bg-amber-950/60 border border-amber-500/40 rounded flex items-center justify-between text-[10px] font-mono text-amber-300 shrink-0">
          <div className="flex items-center gap-1.5 truncate">
            <AlertTriangle className="w-3 h-3 text-amber-400 shrink-0" />
            <span className="truncate">
              {criticalWarnings[0] || `Valid Setup: ${activeSetups.find((s) => s.state === 'SETUP_VALID')?.symbol}`}
            </span>
          </div>
          <span className="text-[9px] text-amber-400 font-bold shrink-0 ml-1">MARKET</span>
        </div>
      )}

      {/* Quick Skill Action Pills */}
      <div className="grid grid-cols-4 gap-1.5 mb-2 shrink-0">
        <button
          onClick={() => handleQuickSkill('windows_app', 'focus', { app_name: 'Notepad' })}
          className="flex items-center justify-center gap-1 py-1 px-1.5 bg-[#09111e] hover:bg-cyan-950/40 text-slate-300 hover:text-cyan-300 border border-slate-800 hover:border-cyan-500/40 rounded text-[10px] font-mono transition-colors truncate"
          title="Focus Notepad"
        >
          <AppWindow className="w-3 h-3 text-cyan-400 shrink-0" />
          <span className="truncate">Focus</span>
        </button>

        <button
          onClick={() => handleQuickSkill('terminal', 'run_command', { command: 'Get-Process | Select-Object -First 5' })}
          className="flex items-center justify-center gap-1 py-1 px-1.5 bg-[#09111e] hover:bg-amber-950/40 text-slate-300 hover:text-amber-300 border border-slate-800 hover:border-amber-500/40 rounded text-[10px] font-mono transition-colors truncate"
          title="Run Safe Terminal Command"
        >
          <Terminal className="w-3 h-3 text-amber-400 shrink-0" />
          <span className="truncate">Terminal</span>
        </button>

        <button
          onClick={() => handleQuickSkill('obsidian', 'search', { query: 'trading risk rules' })}
          className="flex items-center justify-center gap-1 py-1 px-1.5 bg-[#09111e] hover:bg-purple-950/40 text-slate-300 hover:text-purple-300 border border-slate-800 hover:border-purple-500/40 rounded text-[10px] font-mono transition-colors truncate"
          title="Search Obsidian Vault"
        >
          <BookOpen className="w-3 h-3 text-purple-400 shrink-0" />
          <span className="truncate">Obsidian</span>
        </button>

        <button
          onClick={() => handleQuickSkill('browser', 'open_url', { url: 'https://tradingview.com' })}
          className="flex items-center justify-center gap-1 py-1 px-1.5 bg-[#09111e] hover:bg-blue-950/40 text-slate-300 hover:text-blue-300 border border-slate-800 hover:border-blue-500/40 rounded text-[10px] font-mono transition-colors truncate"
          title="Open TradingView"
        >
          <Globe className="w-3 h-3 text-blue-400 shrink-0" />
          <span className="truncate">Browser</span>
        </button>
      </div>

      {/* Deterministic Bypass Indicator */}
      {deterministicPreview && (
        <div className="mb-2 px-2 py-1 bg-cyan-950/70 border border-cyan-500/40 rounded flex items-center justify-between text-[10px] font-mono text-cyan-300 shrink-0">
          <div className="flex items-center gap-1 truncate">
            <Zap className="w-3 h-3 text-cyan-400 shrink-0" />
            <span className="font-bold">DETERMINISTIC:</span>
            <span className="truncate">{deterministicPreview.skill}.{deterministicPreview.action}()</span>
          </div>
          <span className="text-[9px] text-cyan-400/80 uppercase font-semibold">Bypass LLM</span>
        </div>
      )}

      {/* Input & Voice Control Bar */}
      <div className="pt-2 border-t border-slate-800/90 shrink-0">
        <form onSubmit={handleSubmitCommand} className="flex items-center gap-1.5">
          <input
            ref={inputRef}
            type="text"
            placeholder="Run action or ask TARS (Ctrl+Shift+Space)..."
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            disabled={isSubmitting}
            className="flex-1 bg-[#060b14] border border-cyan-500/30 focus:border-cyan-400 rounded-lg px-3 py-2 text-xs font-mono text-slate-100 placeholder-slate-500 focus:outline-none transition-colors"
          />

          <button
            type="submit"
            disabled={!inputText.trim() || isSubmitting}
            className="p-2 bg-cyan-600 hover:bg-cyan-500 active:bg-cyan-700 text-slate-950 rounded-lg transition-colors disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
            title="Execute Action"
          >
            <Send className="w-3.5 h-3.5" />
          </button>

          <button
            type="button"
            onClick={onTogglePushToTalk}
            className={`p-2 rounded-lg font-mono text-xs font-semibold transition-all shrink-0 ${
              isListening
                ? 'bg-emerald-500 text-slate-950 shadow-[0_0_15px_rgba(0,255,102,0.6)] animate-pulse'
                : 'bg-cyan-500/20 hover:bg-cyan-500/30 text-cyan-300 border border-cyan-500/40'
            }`}
            title={isListening ? 'Stop Voice Input' : 'Voice Push-to-Talk (Hold or Click)'}
          >
            <Mic className="w-3.5 h-3.5" />
          </button>
        </form>
      </div>
    </div>
  );
};
