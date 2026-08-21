import React, { useState } from 'react';
import { TARSAssistantMessage } from '../../types/assistant-message';
import { MarkdownContent } from './MarkdownContent';
import { composeSpeech } from '../../services/speech';
import { Copy, Check, Volume2 } from 'lucide-react';

interface AssistantMessageProps {
  message: TARSAssistantMessage;
  isStreaming?: boolean;
  onSpeak?: (text: string) => void;
}

export const AssistantMessage: React.FC<AssistantMessageProps> = ({
  message,
  isStreaming = false,
  onSpeak,
}) => {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(message.content);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleSpeak = () => {
    if (onSpeak) {
      const speechText = composeSpeech(message.content);
      if (speechText) {
        onSpeak(speechText);
      }
    }
  };

  return (
    <div className="flex items-start gap-3.5 w-full group max-w-[820px]">
      {/* TARS Brand Avatar Mark */}
      <div className="w-6 h-6 rounded-md bg-[#1f2937] flex items-center justify-center text-white shrink-0 mt-0.5 shadow-xs">
        <div className="w-3 h-3 rounded-full border border-white" />
      </div>

      {/* Content Area */}
      <div className="flex-1 min-w-0">
        {/* Author Label (Clean TARS mark only) */}
        <div className="flex items-center gap-2 mb-1.5">
          <span className="font-semibold text-xs text-[#1f2937]">TARS</span>
        </div>

        {/* Message Body */}
        <div className="text-[#374151] text-[15px] font-sans leading-[1.6] select-text">
          <MarkdownContent content={message.content} isStreaming={isStreaming} />
        </div>

        {/* Action Toolbar */}
        {!isStreaming && (
          <div className="flex items-center gap-3.5 mt-2.5 opacity-0 group-hover:opacity-100 transition-opacity text-[#9ca3af] text-xs">
            <button
              type="button"
              onClick={handleCopy}
              className="hover:text-[#374151] transition-colors cursor-pointer flex items-center gap-1"
              title="Copy message"
            >
              {copied ? (
                <>
                  <Check className="w-3.5 h-3.5 text-emerald-600" />
                  <span className="text-[11px] text-emerald-600 font-medium">Copied</span>
                </>
              ) : (
                <>
                  <Copy className="w-3.5 h-3.5" />
                  <span className="text-[11px]">Copy</span>
                </>
              )}
            </button>

            {onSpeak && (
              <button
                type="button"
                onClick={handleSpeak}
                className="hover:text-[#374151] transition-colors cursor-pointer flex items-center gap-1"
                title="Read aloud"
              >
                <Volume2 className="w-3.5 h-3.5" />
                <span className="text-[11px]">Speak</span>
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
};
