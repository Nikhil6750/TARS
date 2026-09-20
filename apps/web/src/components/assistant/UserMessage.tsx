import React, { useState } from 'react';
import { TARSAssistantMessage } from '../../types/assistant-message';
import { Mic, Copy, Check } from 'lucide-react';

interface UserMessageProps {
  message: TARSAssistantMessage;
}

export const UserMessage: React.FC<UserMessageProps> = ({ message }) => {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(message.content);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="flex justify-end w-full group">
      <div className="max-w-[85%] sm:max-w-[70%] flex flex-col items-end">
        {/* Clean Light-Gray Message Bubble */}
        <div className="relative px-4 py-3 rounded-2xl bg-[#f4f4f5] border border-[#e5e7eb] text-[#1f2937] text-[15px] font-sans leading-[1.6] shadow-xs select-text">
          <p className="whitespace-pre-wrap">{message.content}</p>
        </div>

        {/* Action icons below bubble on hover */}
        <div className="flex items-center gap-2 mt-1 px-1 opacity-0 group-hover:opacity-100 transition-opacity text-[11px] text-[#9ca3af]">
          {message.input_mode === 'voice' && (
            <span className="flex items-center gap-1">
              <Mic className="w-3 h-3 text-[#6b7280]" />
              <span>Voice</span>
            </span>
          )}
          <button
            type="button"
            onClick={handleCopy}
            className="hover:text-[#374151] transition-colors cursor-pointer flex items-center gap-0.5"
            title="Copy message"
          >
            {copied ? <Check className="w-3 h-3 text-emerald-600" /> : <Copy className="w-3 h-3" />}
          </button>
        </div>
      </div>
    </div>
  );
};
