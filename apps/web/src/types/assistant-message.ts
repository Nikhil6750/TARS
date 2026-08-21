/**
 * Canonical TARS Assistant Message Types
 * Generated & mirrored from contracts/assistant-message.schema.json (v1.0.0)
 * and assistant V2 presentation contracts.
 */

export type AssistantRole = 'user' | 'assistant' | 'system';

export type InputMode = 'text' | 'voice';

export interface AssistantProviders {
  stt?: string | null;
  assistant?: string | null;
  tts?: string | null;
}

export interface TARSAssistantMessage {
  schema_version: '1.0.0';
  message_id: string;
  conversation_id: string;
  timestamp: string;
  role: AssistantRole;
  content: string;
  input_mode: InputMode;
  audio_ref?: string | null;
  related_event_id?: string | null;
  intent?: string | null;
  providers?: AssistantProviders;
  error?: string | null;
  display_text?: string;
  speech_text?: string;
}

export interface AssistantResponseQuality {
  directness?: boolean;
  completeness?: boolean;
  grounding?: boolean;
  uncertainty?: boolean;
  structure?: boolean;
  user_mode_cleanliness?: boolean;
  speech_suitability?: boolean;
  passed?: number;
  issues?: string[];
}

export interface AssistantResponseV2 {
  message: TARSAssistantMessage;
  display_text: string;
  speech_text: string;
  quality?: AssistantResponseQuality;
}
