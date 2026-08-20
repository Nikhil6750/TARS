import React, { useState, useRef, useEffect } from 'react';
import { Mic, ArrowUp, Paperclip, Square } from 'lucide-react';

interface ComposerProps {
  onSendMessage: (text: string, inputMode?: 'text' | 'voice') => void;
  isListening: boolean;
  onTogglePushToTalk: () => void;
  disabled?: boolean;
  placeholder?: string;
  onAttachFile?: () => void;
}

export const Composer: React.FC<ComposerProps> = ({
  onSendMessage,
  isListening,
  onTogglePushToTalk,
  disabled = false,
  placeholder = 'Ask TARS...',
  onAttachFile,
}) => {
  const [text, setText] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Auto resize textarea height based on content up to 140px
  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = 'auto';
    const nextHeight = Math.min(textarea.scrollHeight, 140);
    textarea.style.height = `${Math.max(nextHeight, 26)}px`;
  }, [text]);

  const handleSubmit = () => {
    const trimmed = text.trim();
    if (trimmed && !disabled) {
      onSendMessage(trimmed, 'text');
      setText('');
      if (textareaRef.current) {
        textareaRef.current.style.height = '26px';
      }
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const canSend = text.trim().length > 0 && !disabled;

  return (
    <div className="w-full select-none flex flex-col items-center max-w-[820px] mx-auto">
      {/* Stadium-Shaped Floating Input Bar Matching Reference */}
      <div
        className={`w-full min-h-[58px] relative flex items-center gap-3 px-4 sm:px-5 py-2.5 rounded-[28px] bg-white border transition-all shadow-[0_4px_24px_rgba(0,0,0,0.06)] ${
          isListening
            ? 'border-emerald-500 ring-2 ring-emerald-500/20'
            : 'border-[#e5e7eb] hover:border-[#d1d5db] focus-within:border-[#9ca3af] focus-within:shadow-[0_6px_28px_rgba(0,0,0,0.09)]'
        }`}
      >
        {/* Attachment Paperclip Icon Matching Reference */}
        <button
          type="button"
          onClick={onAttachFile || (() => onSendMessage('Analyze this chart', 'text'))}
          title="Attach or analyze chart"
          className="p-2 rounded-full text-[#9ca3af] hover:text-[#374151] hover:bg-[#f3f4f6] transition-colors cursor-pointer shrink-0"
        >
          <Paperclip className="w-4 h-4 stroke-[1.8]" />
        </button>

        {/* Input Textarea */}
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={disabled}
          placeholder={isListening ? 'Listening to voice command...' : placeholder}
          rows={1}
          className="flex-1 bg-transparent border-0 outline-none resize-none text-[15px] text-[#1f2937] placeholder-[#9ca3af] font-sans leading-relaxed py-1 max-h-[140px] custom-scrollbar select-text"
        />

        {/* Right Action Icons: Microphone + Up-Arrow Send Button */}
        <div className="flex items-center gap-2 shrink-0">
          {/* Microphone Icon Button (~40px) */}
          <button
            type="button"
            onClick={onTogglePushToTalk}
            title={isListening ? 'Stop listening (click or Esc)' : 'Push to talk (Ctrl+Shift+V)'}
            className={`w-10 h-10 rounded-full flex items-center justify-center transition-all cursor-pointer ${
              isListening
                ? 'bg-emerald-500 text-white animate-pulse shadow-sm'
                : 'text-[#4b5563] hover:text-[#111827] hover:bg-[#f3f4f6]'
            }`}
          >
            {isListening ? (
              <Square className="w-4 h-4 fill-current stroke-none" />
            ) : (
              <Mic className="w-4 h-4 stroke-[1.8]" />
            )}
          </button>

          {/* Up-Arrow Send Button (~40px) Matching Reference */}
          <button
            type="button"
            onClick={handleSubmit}
            disabled={!canSend}
            title="Send message"
            className={`w-10 h-10 rounded-full flex items-center justify-center transition-all cursor-pointer ${
              canSend
                ? 'bg-[#1f2937] text-white hover:bg-[#111827] shadow-xs'
                : 'bg-[#f3f4f6] text-[#d1d5db] cursor-not-allowed'
            }`}
          >
            <ArrowUp className="w-4 h-4 stroke-[2.4]" />
          </button>
        </div>
      </div>

      {/* Small Disclaimer Matching Reference */}
      <p className="mt-3 text-[11px] text-[#9ca3af] text-center font-normal tracking-tight select-none">
        TARS provides general information, not financial advice. Verify important information.
      </p>
    </div>
  );
};
