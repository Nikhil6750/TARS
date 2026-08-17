import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  Maximize2,
  Minimize2,
  Mic,
  Send,
  Zap,
  Terminal,
  AlertTriangle,
  Camera,
  Layers,
  Sparkles,
  X,
  Radio,
} from 'lucide-react';
import { TARSOrb } from '../character/TARSOrb';
import { CompanionVisualState } from '../../types/companion';
import { TARSTradingEvent } from '../../types/trading-event';
import {
  ActionRequest,
  ActionResult,
  ActiveWindowContext,
  BrowserPageContext,
  MonitorInfo,
  MultiStepActionPlan,
  ScreenCaptureResult,
  UIElementNode,
} from '../../types/actions';
import { actionRuntimeClient } from '../../services/actions';
import { nativeBridge } from '../../services/native-bridge';
import { browserControlService } from '../../services/browser-control';
import { actionPlannerService } from '../../services/action-planner';
import { frontendCommandBridge } from '../../services/frontend-command-bridge';
import { visualTargetingService } from '../../services/visual-targeting';
import { WakeWordStatusInfo } from '../../services/wake-word';
import { ActiveContextBar } from './ActiveContextBar';
import { ActionConfirmationCard } from './ActionConfirmationCard';
import { ActionResultView } from './ActionResultView';
import { VisualInspectorCard } from './VisualInspectorCard';
import { BrowserContextCard } from './BrowserContextCard';
import { MultiStepPlanView } from './MultiStepPlanView';
import { ChartAnalysisCard, ChartAnalysisData } from './ChartAnalysisCard';

interface HUDOverlayProps {
  companionState: CompanionVisualState;
  onExpand: () => void;
  onHideHUD?: () => void;
  activeSetups: TARSTradingEvent[];
  criticalWarnings: string[];
  isListening: boolean;
  onTogglePushToTalk: () => void;
  audioVolume: number;
  apiEndpoint?: string;
  onSendMessage?: (text: string) => void;
  onAnalyzeChart?: () => Promise<void> | void;
  latestChartAnalysis?: ChartAnalysisData | null;
  onClearChartAnalysis?: () => void;
  wakeStatus?: WakeWordStatusInfo;
  onToggleWakeListening?: () => void;
  liveTranscript?: string;
  isAnalyzingChart?: boolean;
  streamedAnalysisText?: string;
}

export const HUDOverlay: React.FC<HUDOverlayProps> = ({
  companionState,
  onExpand,
  onHideHUD,
  activeSetups,
  criticalWarnings,
  isListening,
  onTogglePushToTalk,
  audioVolume,
  apiEndpoint = 'http://127.0.0.1:8000',
  onSendMessage,
  onAnalyzeChart,
  latestChartAnalysis,
  onClearChartAnalysis,
  wakeStatus,
  onToggleWakeListening,
  liveTranscript,
  isAnalyzingChart = false,
  streamedAnalysisText,
}) => {
  // Active foreground window context & monitors
  const [activeContext, setActiveContext] = useState<ActiveWindowContext | null>(null);
  const [monitors, setMonitors] = useState<MonitorInfo[]>([]);
  const [isRefreshingContext, setIsRefreshingContext] = useState(false);

  // Vision & Screen Awareness State
  const [latestCapture, setLatestCapture] = useState<ScreenCaptureResult | null>(null);
  const [uiTree, setUiTree] = useState<UIElementNode | null>(null);
  const [showVisualInspector, setShowVisualInspector] = useState(false);
  const [isCapturing, setIsCapturing] = useState(false);

  // Browser Context State
  const [browserContext, setBrowserContext] = useState<BrowserPageContext>(
    browserControlService.inspectPage()
  );
  const [showBrowserCard, setShowBrowserCard] = useState(false);

  // Multi-Step Action Planner State
  const [activePlan, setActivePlan] = useState<MultiStepActionPlan | null>(null);
  const [isExecutingPlan, setIsExecutingPlan] = useState(false);

  // Command / Prompt Input
  const [inputText, setInputText] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);

  // Action Runtime States
  const [pendingConfirmation, setPendingConfirmation] = useState<ActionRequest | null>(null);
  const [pendingConfirmationToken, setPendingConfirmationToken] = useState<string | null>(null);
  const [latestResult, setLatestResult] = useState<ActionResult | null>(null);

  const inputRef = useRef<HTMLInputElement>(null);

  // Refresh active window context & monitors on mount or summon
  const refreshActiveContext = useCallback(async () => {
    setIsRefreshingContext(true);
    try {
      const [ctx, mons, tree] = await Promise.all([
        nativeBridge.getActiveWindowContext(),
        nativeBridge.getMonitorsGeometry(),
        nativeBridge.getActiveWindowElements(),
      ]);
      if (ctx) setActiveContext(ctx);
      if (mons) setMonitors(mons);
      if (tree) setUiTree(tree);

      const bCtx = browserControlService.inspectPage();
      setBrowserContext(bCtx);
    } catch (err) {
      console.warn('[HUDOverlay] Failed to refresh context:', err);
    } finally {
      setIsRefreshingContext(false);
    }
  }, []);

  useEffect(() => {
    refreshActiveContext();
    inputRef.current?.focus();
  }, [refreshActiveContext]);

  // Handle Esc key to hide HUD to tray
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        if (onHideHUD) {
          onHideHUD();
        } else {
          nativeBridge.hideHUD();
        }
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onHideHUD]);

  // Subscribe to action results and plan updates
  useEffect(() => {
    actionRuntimeClient.setEndpoint(apiEndpoint);
    frontendCommandBridge.connect(apiEndpoint);

    const unsubAction = actionRuntimeClient.onAnyActionResult((result, request) => {
      setLatestResult(result);
      if (result.status === 'CONFIRMATION_REQUIRED' && request) {
        setPendingConfirmation(request);
        const token = result.data?.confirmation_token;
        setPendingConfirmationToken(typeof token === 'string' ? token : null);
      } else if (result.status !== 'CONFIRMATION_REQUIRED' && result.status !== 'PENDING') {
        if (pendingConfirmation && pendingConfirmation.id === result.request_id) {
          setPendingConfirmation(null);
          setPendingConfirmationToken(null);
        }
      }
    });

    const unsubPlan = actionPlannerService.onPlanUpdate((plan) => {
      setActivePlan(plan);
    });

    return () => {
      unsubAction();
      unsubPlan();
    };
  }, [apiEndpoint, pendingConfirmation]);

  // Capture screen snapshot
  const handleCaptureSnapshot = async () => {
    setIsCapturing(true);
    try {
      const cap = await nativeBridge.captureActiveWindow(true);
      setLatestCapture(cap);
      setShowVisualInspector(true);
      setShowBrowserCard(false);
    } catch (err) {
      console.error('[HUDOverlay] Screen capture error:', err);
    } finally {
      setIsCapturing(false);
    }
  };

  // Browser navigation handler
  const handleBrowserNavigate = async (url: string) => {
    const res = await browserControlService.navigate(url);
    const updated = browserControlService.inspectPage();
    setBrowserContext(updated);
    setLatestResult(res);
  };

  // Multi-step Plan Execution
  const handleExecutePlan = async () => {
    setIsExecutingPlan(true);
    try {
      const res = await actionPlannerService.executePlan();
      setLatestResult(res);
    } finally {
      setIsExecutingPlan(false);
    }
  };

  const handleResumePlan = async (approved: boolean) => {
    setIsExecutingPlan(true);
    try {
      const res = await actionPlannerService.resumeAfterConfirmation(approved);
      setLatestResult(res);
    } finally {
      setIsExecutingPlan(false);
    }
  };

  // Deterministic command preview
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
      // 1. Check for chart analysis keyword
      if (/\b(analy[sz]e)\s+(this|the|my)?\s*chart\b/i.test(text)) {
        setInputText('');
        if (onAnalyzeChart) {
          await onAnalyzeChart();
          return;
        }
      }

      // 2. Check for multi-step workflow command
      const researchMatch = text.match(/^(?:research|analyze|find\s+setup\s+for)\s+([a-zA-Z0-9_-]+)$/i);
      if (researchMatch) {
        const symbol = researchMatch[1].toUpperCase();
        const plan = actionPlannerService.createDeterministicWorkflow('market_research', { symbol });
        setActivePlan(plan);
        setInputText('');
        return;
      }

      // 3. Check for deterministic command (bypasses LLM)
      const deterministicReq = actionRuntimeClient.parseDeterministicCommand(
        text,
        activeContext,
        'hud'
      );

      if (deterministicReq) {
        setInputText('');
        if (deterministicReq.skill === 'windows_app' && deterministicReq.action === 'capture_active_window') {
          await handleCaptureSnapshot();
          return;
        }
        if (deterministicReq.skill === 'browser' && deterministicReq.action === 'open_url') {
          const url = String(deterministicReq.arguments.url || '');
          await handleBrowserNavigate(url);
          setShowBrowserCard(true);
          return;
        }

        const res = await actionRuntimeClient.submitAction(deterministicReq);
        if (res.status === 'CONFIRMATION_REQUIRED') {
          setPendingConfirmation(deterministicReq);
          const token = res.data?.confirmation_token;
          setPendingConfirmationToken(typeof token === 'string' ? token : null);
        }
      } else {
        // 4. Visual/UI Target Query Resolution
        const isTargeting = /^(?:click|find|press|select|focus|tap)\s+/i.test(text);
        if (isTargeting) {
          const resolution = visualTargetingService.resolveTarget(
            { query: text },
            {
              domTree: browserContext.dom_tree,
              uiTree: uiTree || undefined,
              monitors,
              windowBounds: activeContext?.window_bounds,
              activeExecutable: activeContext?.executable,
            }
          );

          if (resolution.target_type !== 'unresolved' && resolution.proposed_action) {
            setInputText('');
            const req = actionRuntimeClient.createRequest({
              skill: resolution.proposed_action.skill as ActionRequest['skill'],
              action: resolution.proposed_action.action,
              arguments: resolution.proposed_action.arguments,
              source: 'hud',
              activeContext,
            });
            const res = await actionRuntimeClient.submitAction(req);
            setLatestResult(res);
            return;
          }
        }

        // 5. Normal Assistant Chat / Intent Query
        if (onSendMessage) {
          onSendMessage(text);
          setInputText('');
        }
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  // Confirm / Deny Pending Action
  const handleConfirmAction = async () => {
    if (!pendingConfirmation || !pendingConfirmationToken) return;
    try {
      const res = await actionRuntimeClient.respondToConfirmation(
        pendingConfirmation.id,
        pendingConfirmationToken,
        true
      );
      setPendingConfirmation(null);
      setPendingConfirmationToken(null);
      setLatestResult(res);
    } catch (err) {
      console.error('[HUDOverlay] Failed to confirm action:', err);
    }
  };

  const handleDenyAction = async () => {
    if (pendingConfirmation && pendingConfirmationToken) {
      try {
        const res = await actionRuntimeClient.respondToConfirmation(
          pendingConfirmation.id,
          pendingConfirmationToken,
          false
        );
        setLatestResult(res);
      } catch (err) {
        console.error('[HUDOverlay] Failed to deny action:', err);
      }
    }
    setPendingConfirmation(null);
    setPendingConfirmationToken(null);
  };

  const handleQuickSkill = async (skill: string, action: string, args: Record<string, unknown> = {}) => {
    const req = actionRuntimeClient.createRequest({
      skill: skill as ActionRequest['skill'],
      action,
      arguments: args,
      source: 'hud',
      activeContext,
    });
    const res = await actionRuntimeClient.submitAction(req);
    if (res.status === 'CONFIRMATION_REQUIRED') {
      setPendingConfirmation(req);
      const token = res.data?.confirmation_token;
      setPendingConfirmationToken(typeof token === 'string' ? token : null);
    } else {
      setLatestResult(res);
    }
  };

  const handleTriggerAnalyzeChart = async () => {
    if (onAnalyzeChart) {
      await onAnalyzeChart();
    }
  };

  return (
    <div
      data-testid="hud-overlay"
      className="w-full h-full flex flex-col bg-[#03060a]/98 text-slate-100 font-sans select-none border border-cyan-500/30 rounded-xl p-2.5 shadow-[0_0_40px_rgba(0,240,255,0.15)] backdrop-blur-xl relative overflow-hidden"
    >
      {/* HUD Header Bar */}
      <div className="flex items-center justify-between pb-2 border-b border-cyan-500/20 shrink-0">
        <div className="flex items-center gap-2">
          <div className="w-2.5 h-2.5 rounded-full bg-cyan-400 animate-pulse shadow-[0_0_8px_#00f0ff]" />
          <div className="flex flex-col">
            <span className="font-mono text-xs font-bold tracking-widest text-cyan-300">
              TARS HUD
            </span>
            <span className="font-mono text-[9px] text-slate-400">
              WAVE 2B · VOICE & VISION
            </span>
          </div>
        </div>

        {/* Center: Wake-word Listener Status Pill */}
        {wakeStatus && (
          <button
            type="button"
            onClick={onToggleWakeListening}
            className={`flex items-center gap-1 px-2 py-0.5 rounded-full border text-[9px] font-mono transition-all ${
              wakeStatus.isActive
                ? 'bg-emerald-950/60 border-emerald-500/50 text-emerald-300 shadow-[0_0_8px_rgba(16,185,129,0.3)]'
                : 'bg-slate-900 border-slate-700 text-slate-400 hover:border-slate-500'
            }`}
            title={wakeStatus.isActive ? 'Local Wake Listener: Active ("Hey TARS") - Click to mute' : 'Local Wake Listener: Muted - Click to activate'}
          >
            <Radio className={`w-2.5 h-2.5 ${wakeStatus.isActive ? 'text-emerald-400 animate-pulse' : 'text-slate-500'}`} />
            <span>{wakeStatus.isActive ? 'WAKE: ON' : 'WAKE: OFF'}</span>
          </button>
        )}

        {/* Actions: Expand / Hide / Close */}
        <div className="flex items-center gap-1 text-slate-400">
          <button
            onClick={onExpand}
            className="p-1 hover:text-cyan-300 hover:bg-slate-800/60 rounded transition-colors"
            title="Expand to Full Workstation"
          >
            <Maximize2 className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={() => {
              if (onHideHUD) onHideHUD();
              else nativeBridge.hideHUD();
            }}
            className="p-1 hover:text-amber-300 hover:bg-slate-800/60 rounded transition-colors"
            title="Hide HUD to Tray (Esc)"
          >
            <Minimize2 className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={() => {
              if (onHideHUD) onHideHUD();
              else nativeBridge.hideHUD();
            }}
            className="p-1 hover:text-rose-400 hover:bg-rose-950/40 rounded transition-colors"
            title="Close to Background Tray"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {/* Active Foreground Window Bar */}
      <div className="mt-1.5 shrink-0">
        <ActiveContextBar
          activeContext={activeContext}
          onRefresh={refreshActiveContext}
          isLoading={isRefreshingContext}
        />
      </div>

      {/* Center Interactive Stage */}
      <div className="flex-1 my-1.5 overflow-y-auto space-y-2 pr-0.5 custom-scrollbar min-h-0 flex flex-col justify-start">
        {/* Visual Inspector Drawer */}
        {showVisualInspector ? (
          <VisualInspectorCard
            capture={latestCapture}
            uiTree={uiTree}
            monitors={monitors}
            onRefreshCapture={handleCaptureSnapshot}
            onClose={() => setShowVisualInspector(false)}
            isLoading={isCapturing}
          />
        ) : showBrowserCard ? (
          /* Browser Automation Card */
          <BrowserContextCard
            context={browserContext}
            onNavigate={handleBrowserNavigate}
            onClose={() => setShowBrowserCard(false)}
          />
        ) : activePlan ? (
          /* Multi-Step Action Sequence Plan */
          <MultiStepPlanView
            plan={activePlan}
            onExecute={handleExecutePlan}
            onResume={handleResumePlan}
            onCancel={() => actionPlannerService.clearPlan()}
            isExecuting={isExecutingPlan}
          />
        ) : pendingConfirmation ? (
          /* Action Confirmation Card */
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
          /* Primary TARS Quantum Voice Core & Hero Visualizer */
          <div className="flex flex-col items-center justify-start py-1 flex-1">
            <TARSOrb
              state={companionState}
              audioVolume={audioVolume}
              size="hud"
              onClick={onTogglePushToTalk}
            />

            {/* Live Transcript / Speech Indicator */}
            {liveTranscript && isListening && (
              <div className="mt-2 px-3 py-1 bg-slate-900/90 border border-emerald-500/40 rounded-full text-[11px] font-mono text-emerald-300 animate-pulse text-center max-w-[90%] truncate">
                &ldquo;{liveTranscript}&rdquo;
              </div>
            )}

            {/* Progressive Live Streaming Text Generation Card */}
            {streamedAnalysisText ? (
              <div className="mt-2.5 w-full bg-[#050c18]/95 border border-cyan-500/40 rounded-xl p-3 shadow-[0_0_25px_rgba(0,240,255,0.15)] backdrop-blur-md animate-fade-in">
                <div className="flex items-center justify-between pb-1.5 mb-1.5 border-b border-cyan-500/20 text-[10px] font-mono text-cyan-300">
                  <div className="flex items-center gap-1.5">
                    <div className="w-2 h-2 rounded-full bg-cyan-400 animate-ping" />
                    <span className="font-bold">ANALYZING ACTIVE CHART...</span>
                  </div>
                  <span className="text-[9px] text-slate-400">Claude Code · Live</span>
                </div>
                <div className="font-mono text-[11px] text-slate-200 whitespace-pre-wrap leading-relaxed max-h-48 overflow-y-auto custom-scrollbar">
                  {streamedAnalysisText}
                  <span className="inline-block w-2 h-3 bg-cyan-400 animate-pulse ml-0.5 align-middle" />
                </div>
              </div>
            ) : latestChartAnalysis ? (
              /* Structured Chart Analysis Read */
              <div className="mt-2 w-full">
                <ChartAnalysisCard
                  analysis={latestChartAnalysis}
                  onDismiss={onClearChartAnalysis}
                  isSpeaking={companionState === 'SPEAKING'}
                />
              </div>
            ) : null}

            {/* Primary Demo Trigger: Analyze Active Chart Button */}
            <div className="mt-3 w-full px-1">
              <button
                type="button"
                onClick={handleTriggerAnalyzeChart}
                disabled={isAnalyzingChart || companionState === 'THINKING'}
                className="w-full group relative flex items-center justify-center gap-2 py-2 px-3 bg-gradient-to-r from-cyan-950/80 via-[#0a182c] to-cyan-950/80 hover:from-cyan-900 hover:to-cyan-900 border border-cyan-500/50 hover:border-cyan-400 rounded-xl text-xs font-mono font-bold text-cyan-200 hover:text-cyan-100 shadow-[0_0_20px_rgba(0,240,255,0.2)] hover:shadow-[0_0_30px_rgba(0,240,255,0.4)] transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <div className="absolute inset-0 rounded-xl bg-cyan-400/5 group-hover:bg-cyan-400/10 transition-colors" />
                <Sparkles className="w-3.5 h-3.5 text-cyan-400 group-hover:scale-110 transition-transform" />
                <span>
                  {isAnalyzingChart || companionState === 'THINKING'
                    ? 'ANALYZING ACTIVE CHART...'
                    : '⚡ ANALYZE ACTIVE CHART'}
                </span>
                <span className="ml-1 text-[9px] px-1.5 py-0.5 rounded bg-cyan-950 border border-cyan-500/40 text-cyan-300 font-normal">
                  Say &quot;Analyze this chart&quot;
                </span>
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Active Market Signal / Warning Banner */}
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

      {/* Quick Wave 2B Skill Action Pills */}
      <div className="grid grid-cols-4 gap-1.5 mb-1.5 shrink-0">
        <button
          onClick={handleCaptureSnapshot}
          className="flex items-center justify-center gap-1 py-1 px-1.5 bg-[#09111e] hover:bg-cyan-950/40 text-slate-300 hover:text-cyan-300 border border-slate-800 hover:border-cyan-500/40 rounded text-[10px] font-mono transition-colors truncate"
          title="Capture Active Window Snapshot"
        >
          <Camera className="w-3 h-3 text-cyan-400 shrink-0" />
          <span className="truncate">Capture</span>
        </button>

        <button
          onClick={() => {
            const updated = browserControlService.inspectPage();
            setBrowserContext(updated);
            setShowBrowserCard(true);
            setShowVisualInspector(false);
          }}
          className="flex items-center justify-center gap-1 py-1 px-1.5 bg-[#09111e] hover:bg-blue-950/40 text-slate-300 hover:text-blue-300 border border-slate-800 hover:border-blue-500/40 rounded text-[10px] font-mono transition-colors truncate"
          title="Inspect DOM & Browser Context"
        >
          <Layers className="w-3 h-3 text-blue-400 shrink-0" />
          <span className="truncate">DOM</span>
        </button>

        <button
          onClick={() => {
            const plan = actionPlannerService.createDeterministicWorkflow('market_research', { symbol: 'AAPL' });
            setActivePlan(plan);
          }}
          className="flex items-center justify-center gap-1 py-1 px-1.5 bg-[#09111e] hover:bg-emerald-950/40 text-slate-300 hover:text-emerald-300 border border-slate-800 hover:border-emerald-500/40 rounded text-[10px] font-mono transition-colors truncate"
          title="Run Multi-Step Market Research Workflow"
        >
          <Sparkles className="w-3 h-3 text-emerald-400 shrink-0" />
          <span className="truncate">Workflow</span>
        </button>

        <button
          onClick={() => handleQuickSkill('terminal', 'run_command', { command: 'Get-Process | Select-Object -First 5' })}
          className="flex items-center justify-center gap-1 py-1 px-1.5 bg-[#09111e] hover:bg-amber-950/40 text-slate-300 hover:text-amber-300 border border-slate-800 hover:border-amber-500/40 rounded text-[10px] font-mono transition-colors truncate"
          title="Run Safe Terminal Command"
        >
          <Terminal className="w-3 h-3 text-amber-400 shrink-0" />
          <span className="truncate">Terminal</span>
        </button>
      </div>

      {/* Deterministic Bypass Indicator */}
      {deterministicPreview && (
        <div className="mb-1.5 px-2 py-1 bg-cyan-950/70 border border-cyan-500/40 rounded flex items-center justify-between text-[10px] font-mono text-cyan-300 shrink-0">
          <div className="flex items-center gap-1 truncate">
            <Zap className="w-3 h-3 text-cyan-400 shrink-0" />
            <span className="font-bold">DETERMINISTIC:</span>
            <span className="truncate">{deterministicPreview.skill}.{deterministicPreview.action}()</span>
          </div>
          <span className="text-[9px] text-cyan-400/80 uppercase font-semibold">Bypass LLM</span>
        </div>
      )}

      {/* Input & Voice Control Bar */}
      <div className="pt-1.5 border-t border-slate-800/90 shrink-0">
        <form onSubmit={handleSubmitCommand} className="flex items-center gap-1.5">
          <input
            ref={inputRef}
            type="text"
            placeholder="Run action, target element, or ask TARS..."
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

        {/* Subtle Hotkey Reference */}
        <div className="mt-1.5 flex items-center justify-between text-[9px] font-mono text-slate-400 px-0.5">
          <span>Wake: &quot;Hey TARS&quot;</span>
          <span>Summon: Ctrl+Shift+Space</span>
          <span>Close: Esc</span>
        </div>
      </div>
    </div>
  );
};
