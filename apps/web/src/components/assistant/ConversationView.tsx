import React, { useEffect, useRef } from 'react';
import { TARSAssistantMessage } from '../../types/assistant-message';
import { CompanionVisualState } from '../../types/companion';
import { UserMessage } from './UserMessage';
import { AssistantMessage } from './AssistantMessage';
import { EmptyState } from './EmptyState';
import { Composer } from './Composer';

interface ConversationViewProps {
  messages: TARSAssistantMessage[];
  streamingAnswer: string;
  analysisProgress?: string;
  companionState: CompanionVisualState;
  isListening: boolean;
  onTogglePushToTalk: () => void;
  onSendMessage: (text: string, inputMode?: 'text' | 'voice') => void;
  onOpenWorkspace: () => void;
  onSpeak?: (text: string) => void;
}

export const ConversationView: React.FC<ConversationViewProps> = ({
  messages,
  streamingAnswer,
  analysisProgress,
  companionState,
  isListening,
  onTogglePushToTalk,
  onSendMessage,
  onOpenWorkspace,
  onSpeak,
}) => {
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const endRef = useRef<HTMLDivElement>(null);

  // Auto scroll on new messages or streamed deltas
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages.length, streamingAnswer, analysisProgress]);

  const isEmpty = messages.length === 0 && !streamingAnswer && !analysisProgress;
  const isThinking = companionState === 'THINKING' || companionState === 'WAKE';

  // Construct in-flight synthetic message if streaming or analyzing
  const inFlightText = streamingAnswer || analysisProgress || (isThinking ? 'Thinking...' : '');

  return (
    <div className="w-full h-full flex flex-col overflow-hidden relative bg-white">
      {/* Scrollable Conversation Center Area */}
      <div
        ref={scrollContainerRef}
        className="flex-1 overflow-y-auto custom-scrollbar px-6 sm:px-10 pt-6 pb-6"
      >
        <div className="max-w-[820px] mx-auto min-h-full flex flex-col justify-start">
          {isEmpty ? (
            <EmptyState
              onSelectPrompt={(prompt) => onSendMessage(prompt, 'text')}
              onOpenWorkspace={onOpenWorkspace}
              isListening={isListening}
            />
          ) : (
            <div className="space-y-6 pt-2 pb-8">
              {messages.map((msg) =>
                msg.role === 'user' ? (
                  <UserMessage key={msg.message_id} message={msg} />
                ) : (
                  <AssistantMessage key={msg.message_id} message={msg} onSpeak={onSpeak} />
                )
              )}

              {/* In-Flight Streaming Message */}
              {(streamingAnswer || analysisProgress || (isThinking && messages[messages.length - 1]?.role === 'user')) && (
                <AssistantMessage
                  message={{
                    schema_version: '1.0.0',
                    message_id: 'in_flight_stream',
                    conversation_id: 'stream',
                    timestamp: new Date().toISOString(),
                    role: 'assistant',
                    content: inFlightText,
                    input_mode: 'text',
                  }}
                  isStreaming={true}
                />
              )}

              <div ref={endRef} className="h-2" />
            </div>
          )}
        </div>
      </div>

      {/* Persistent Bottom Composer Area */}
      <div className="shrink-0 px-6 sm:px-10 pb-5 pt-2 bg-gradient-to-t from-white via-white/95 to-transparent">
        <div className="max-w-[820px] mx-auto">
          <Composer
            onSendMessage={onSendMessage}
            isListening={isListening}
            onTogglePushToTalk={onTogglePushToTalk}
            disabled={isListening}
          />
        </div>
      </div>
    </div>
  );
};
