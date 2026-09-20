export type TurnIntent =
  | 'DETERMINISTIC'
  | 'NORMAL_CONVERSATION'
  | 'CHART_ANALYSIS'
  | 'TOOL_TASK'
  | 'RESEARCH'
  | 'TRADING_RESEARCH';

export type TurnStatus = 'completed' | 'awaiting_command' | 'ignored' | 'failed';

export interface AssistantResponse {
  turn_id: string;
  display_text: string;
  speech_text: string;
  intent: TurnIntent;
  status: TurnStatus;
  provider: string;
  latency_ms: number;
  conversation_id: string;
  transcript?: string | null;
  replayed: boolean;
  audio_chunks_base64: string[];
}
