import React, { useEffect, useRef, useState } from 'react';
import { Mic, Send, Bot, User } from 'lucide-react';
import { TARSAssistantMessage } from '../../types/assistant-message';
import { CompanionVisualState, ConnectionStatus } from '../../types/companion';

interface TarsChatScreenProps {
  messages: TARSAssistantMessage[];
  streamingAnswer: string;
  companionState: CompanionVisualState;
  connectionStatus: ConnectionStatus;
  isListening: boolean;
  onTogglePushToTalk: () => void;
  onSendMessage: (text: string, inputMode?: 'text' | 'voice') => void;
}

const STATE_LABEL: Partial<Record<CompanionVisualState, string>> = {
  THINKING: 'Thinking...',
  SPEAKING: 'Speaking...',
  LISTENING: 'Listening...',
  WAKE: 'Waking up...',
};

/**
 * The default TARS experience: a clean, chat-first assistant screen
 * (OpenJarvis-style) -- identity + LIVE status, conversation transcript,
 * streamed response, input with a mic button. Deliberately minimal: no
 * setup cards, no risk tables, no system metrics. Those live in Workspace
 * (see WorkspaceView.tsx) -- demoted, not deleted.
 */
export const TarsChatScreen: React.FC<TarsChatScreenProps> = ({
  messages,
  streamingAnswer,
  companionState,
  connectionStatus,
  isListening,
  onTogglePushToTalk,
  onSendMessage,
}) => {
  const [inputText, setInputText] = useState('');
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, streamingAnswer]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (inputText.trim()) {
      onSendMessage(inputText.trim(), 'text');
      setInputText('');
    }
  };

  const isLive = connectionStatus === 'connected';
  const stateLabel = STATE_LABEL[companionState];

  return (
    <div className="w-full h-full flex flex-col max-w-3xl mx-auto">
      {/* Header: identity + LIVE/READY state */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-cyan-500/10 shrink-0">
        <span className="font-display-title font-bold text-sm tracking-wider text-slate-100">TARS</span>
        <div className="flex items-center gap-2">
          {stateLabel && <span className="text-[11px] font-mono text-cyan-300">{stateLabel}</span>}
          <span
            className={`flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-mono font-semibold ${
              isLive
                ? 'bg-emerald-950/60 text-emerald-300 border border-emerald-500/30'
                : 'bg-slate-800 text-slate-400 border border-slate-700'
            }`}
          >
            <span className={`w-1.5 h-1.5 rounded-full ${isLive ? 'bg-emerald-400' : 'bg-slate-500'}`} />
            {isLive ? 'LIVE' : 'OFFLINE'}
          </span>
        </div>
      </div>

      {/* Transcript */}
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
        {messages.length === 0 && !streamingAnswer ? (
          <div className="h-full flex flex-col items-center justify-center text-center text-slate-500">
            <Bot className="w-8 h-8 text-cyan-500/30 mb-2" />
            <p className="text-sm font-mono">Say &quot;Hey TARS&quot; or type below to start.</p>
          </div>
        ) : (
          messages.map((msg) => {
            const isUser = msg.role === 'user';
            return (
              <div key={msg.message_id} className={`flex gap-2.5 text-sm ${isUser ? 'justify-end' : 'justify-start'}`}>
                {!isUser && (
                  <div className="w-6 h-6 rounded-md bg-cyan-950/80 border border-cyan-500/40 flex items-center justify-center text-cyan-400 shrink-0 mt-0.5">
                    <Bot className="w-3.5 h-3.5" />
                  </div>
                )}
                <div
                  className={`max-w-[78%] rounded-xl px-3.5 py-2.5 ${
                    isUser
                      ? 'bg-cyan-500/15 border border-cyan-500/25 text-slate-100'
                      : 'bg-[#0d1524] border border-slate-800 text-slate-200'
                  }`}
                >
                  <p className="whitespace-pre-wrap leading-relaxed font-sans">{msg.content}</p>
                </div>
                {isUser && (
                  <div className="w-6 h-6 rounded-md bg-slate-800 border border-slate-700 flex items-center justify-center text-slate-300 shrink-0 mt-0.5">
                    <User className="w-3.5 h-3.5" />
                  </div>
                )}
              </div>
            );
          })
        )}

        {/* Real streamed text as it arrives -- never a fake progress bar,
         * this only appears once genuine deltas have started (see
         * handleSendMessage/streamingAnswer in App.tsx and
         * ChartAnalysisClient's onStatus/onDelta). */}
        {streamingAnswer && (
          <div className="flex gap-2.5 text-sm justify-start">
            <div className="w-6 h-6 rounded-md bg-cyan-950/80 border border-cyan-500/40 flex items-center justify-center text-cyan-400 shrink-0 mt-0.5">
              <Bot className="w-3.5 h-3.5" />
            </div>
            <div className="max-w-[78%] rounded-xl px-3.5 py-2.5 bg-[#0d1524] border border-slate-800 text-slate-200">
              <p className="whitespace-pre-wrap leading-relaxed font-sans">
                {streamingAnswer}
                <span className="inline-block w-1.5 h-3.5 bg-cyan-400 animate-pulse ml-0.5 align-middle" />
              </p>
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      {/* Input */}
      <form onSubmit={handleSubmit} className="flex items-center gap-2 px-4 py-3 border-t border-slate-800 shrink-0">
        <input
          type="text"
          value={inputText}
          onChange={(e) => setInputText(e.target.value)}
          placeholder="Ask TARS..."
          className="flex-1 bg-[#0a0f1a] border border-slate-700/80 rounded-full px-4 py-2.5 text-sm font-sans text-slate-100 placeholder-slate-500 outline-none focus:border-cyan-500"
        />
        <button
          type="button"
          onClick={onTogglePushToTalk}
          title={isListening ? 'Listening... click to send' : 'Push to talk'}
          className={`p-2.5 rounded-full transition-all cursor-pointer shrink-0 ${
            isListening
              ? 'bg-emerald-500 text-slate-950 shadow-[0_0_15px_rgba(0,255,102,0.6)] animate-pulse'
              : 'bg-slate-800 hover:bg-slate-700 text-slate-300'
          }`}
        >
          <Mic className="w-4 h-4" />
        </button>
        <button
          type="submit"
          disabled={!inputText.trim()}
          className="p-2.5 rounded-full bg-cyan-500 hover:bg-cyan-400 disabled:opacity-30 text-slate-950 font-bold transition-all cursor-pointer shrink-0"
        >
          <Send className="w-4 h-4" />
        </button>
      </form>
    </div>
  );
};
