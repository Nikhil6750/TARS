import React, { useState, useRef, useEffect } from 'react';
import {
  MessageSquare,
  Send,
  Mic,
  Volume2,
  VolumeX,
  Bot,
  User,
  ExternalLink
} from 'lucide-react';
import { TARSAssistantMessage } from '../../types/assistant-message';
import { TARSTradingEvent } from '../../types/trading-event';
import { audioService } from '../../services/audio';

interface AskTARSViewProps {
  messages: TARSAssistantMessage[];
  onSendMessage: (text: string, inputMode?: 'text' | 'voice') => void;
  isListening: boolean;
  onTogglePushToTalk: () => void;
  activeSetups: TARSTradingEvent[];
  onInspectSetup: (setup: TARSTradingEvent) => void;
  apiEndpoint?: string;
}

export const AskTARSView: React.FC<AskTARSViewProps> = ({
  messages,
  onSendMessage,
  isListening,
  onTogglePushToTalk,
  activeSetups,
  onInspectSetup,
  apiEndpoint = 'http://127.0.0.1:8000'
}) => {
  const [inputText, setInputText] = useState('');
  const [speakingId, setSpeakingId] = useState<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (inputText.trim()) {
      onSendMessage(inputText.trim(), 'text');
      setInputText('');
    }
  };

  const handleSpeak = async (msg: TARSAssistantMessage) => {
    if (speakingId === msg.message_id) {
      audioService.stopSpeaking();
      setSpeakingId(null);
      return;
    }

    if (!msg.speech_text) return;
    setSpeakingId(msg.message_id);
    try {
      await audioService.synthesizeAndPlay(msg.speech_text, apiEndpoint);
    } catch {
      await audioService.speakText(msg.speech_text);
    } finally {
      setSpeakingId(null);
    }
  };

  return (
    <div className="w-full h-full flex flex-col glass-panel p-4 overflow-hidden max-w-5xl mx-auto bg-[#070e1b]/95">
      {/* Header */}
      <div className="flex items-center justify-between pb-3 border-b border-cyan-500/20">
        <div>
          <h1 className="text-base font-display-title font-bold text-slate-100 flex items-center gap-2">
            <MessageSquare className="w-4 h-4 text-cyan-400" />
            ASK TARS — CONVERSATIONAL INTELLIGENCE
          </h1>
          <p className="text-[11px] font-mono text-slate-400">
            Query market state, calculate deterministic R:R metrics, and review risk boundaries.
          </p>
        </div>
      </div>

      {/* Message Stream */}
      <div className="flex-1 overflow-y-auto my-3 space-y-3 pr-2">
        {messages.length === 0 ? (
          <div className="py-20 text-center flex flex-col items-center justify-center">
            <Bot className="w-10 h-10 text-cyan-500/40 mb-3" />
            <p className="text-sm font-mono text-slate-300">TARS Conversation Stream Initialized</p>
            <p className="text-xs font-mono text-slate-500 max-w-sm mt-1">
              Ask about current Gold setups, aggregate portfolio risk exposure, or strategy confluence parameters.
            </p>
          </div>
        ) : (
          messages.map((msg) => {
            const isUser = msg.role === 'user';
            const isSystem = msg.role === 'system';
            const relatedSetup = msg.related_event_id
              ? activeSetups.find((s) => s.event_id === msg.related_event_id)
              : null;

            return (
              <div
                key={msg.message_id}
                className={`flex gap-3 text-xs font-mono ${
                  isUser ? 'justify-end' : 'justify-start'
                }`}
              >
                {!isUser && (
                  <div className="w-7 h-7 rounded-md bg-cyan-950/80 border border-cyan-500/40 flex items-center justify-center text-cyan-400 shrink-0 mt-0.5">
                    <Bot className="w-4 h-4" />
                  </div>
                )}

                <div
                  className={`max-w-[80%] rounded-xl p-3 border ${
                    isUser
                      ? 'bg-[#0f2038] border-cyan-500/30 text-slate-100'
                      : isSystem
                      ? 'bg-amber-950/40 border-amber-500/40 text-amber-200'
                      : 'bg-[#0a1324] border-slate-800 text-slate-200 shadow-[0_0_12px_rgba(0,0,0,0.5)]'
                  }`}
                >
                  {/* Meta tag / Provider tag */}
                  <div className="flex items-center justify-between gap-4 mb-1 text-[10px] text-slate-500">
                    <span className="font-semibold uppercase tracking-wider">
                      {isUser ? 'TRADER' : isSystem ? 'SYSTEM' : 'TARS COMPANION'}
                    </span>
                    <div className="flex items-center gap-2">
                      {msg.input_mode === 'voice' && (
                        <span className="text-emerald-400 flex items-center gap-0.5">
                          <Mic className="w-2.5 h-2.5" /> VOICE
                        </span>
                      )}
                      {msg.providers?.assistant && (
                        <span className="text-cyan-400/80">{msg.providers.assistant}</span>
                      )}
                      <span>{new Date(msg.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
                    </div>
                  </div>

                  {/* Content */}
                  <p className="text-xs leading-relaxed font-sans text-slate-200 select-text whitespace-pre-wrap">
                    {msg.content}
                  </p>

                  {/* Related Event Badge / Link */}
                  {relatedSetup && (
                    <div className="mt-2.5 pt-2 border-t border-slate-800 flex items-center justify-between bg-[#040810]/60 p-2 rounded">
                      <div className="text-[11px] font-mono text-cyan-300">
                        Related Setup: <span className="font-bold">{relatedSetup.symbol}</span> ({relatedSetup.direction})
                      </div>
                      <button
                        onClick={() => onInspectSetup(relatedSetup)}
                        className="text-[10px] text-cyan-400 hover:underline flex items-center gap-0.5 cursor-pointer"
                      >
                        <span>Inspect</span>
                        <ExternalLink className="w-3 h-3" />
                      </button>
                    </div>
                  )}

                  {/* Audio Speech Playback Button for Assistant */}
                  {!isUser && (
                    <div className="mt-2 pt-1.5 border-t border-slate-800/60 flex items-center justify-end">
                      <button
                        onClick={() => handleSpeak(msg)}
                        className="flex items-center gap-1 text-[10px] text-slate-400 hover:text-cyan-300 transition-colors cursor-pointer"
                      >
                        {speakingId === msg.message_id ? (
                          <>
                            <VolumeX className="w-3 h-3 text-emerald-400 animate-pulse" />
                            <span className="text-emerald-400">Stop Voice</span>
                          </>
                        ) : (
                          <>
                            <Volume2 className="w-3 h-3" />
                            <span>Read Out</span>
                          </>
                        )}
                      </button>
                    </div>
                  )}
                </div>

                {isUser && (
                  <div className="w-7 h-7 rounded-md bg-slate-800 border border-slate-700 flex items-center justify-center text-slate-300 shrink-0 mt-0.5">
                    <User className="w-4 h-4" />
                  </div>
                )}
              </div>
            );
          })
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Input Bar */}
      <form onSubmit={handleSubmit} className="flex items-center gap-2 pt-2 border-t border-slate-800">
        <button
          type="button"
          onClick={onTogglePushToTalk}
          className={`p-2.5 rounded-lg font-mono text-xs font-semibold flex items-center gap-1.5 transition-all cursor-pointer ${
            isListening
              ? 'bg-emerald-500 text-slate-950 shadow-[0_0_15px_rgba(0,255,102,0.6)] animate-pulse'
              : 'bg-cyan-500/20 hover:bg-cyan-500/30 text-cyan-300 border border-cyan-500/40'
          }`}
          title={isListening ? 'Listening... click to send' : 'Push to talk'}
        >
          <Mic className="w-4 h-4" />
          <span className="hidden sm:inline">{isListening ? 'RECORDING' : 'PTT'}</span>
        </button>

        <input
          type="text"
          value={inputText}
          onChange={(e) => setInputText(e.target.value)}
          placeholder="Ask TARS about setups, risk boundaries, or invalidations..."
          className="flex-1 bg-[#050912] border border-slate-700/80 rounded-lg px-3 py-2 text-xs font-sans text-slate-100 placeholder-slate-500 outline-none focus:border-cyan-500"
        />

        <button
          type="submit"
          disabled={!inputText.trim()}
          className="p-2.5 rounded-lg bg-cyan-500 hover:bg-cyan-400 disabled:opacity-30 text-slate-950 font-bold transition-all cursor-pointer"
        >
          <Send className="w-4 h-4" />
        </button>
      </form>
    </div>
  );
};
