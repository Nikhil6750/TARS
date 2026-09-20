/**
 * The ONE authoritative mapping from backend/session facts to the orb's visual state.
 * React never invents a state: every input below is a fact reported by the backend voice
 * session, the realtime transport or the event stream.
 */
export type OrbState =
  | 'IDLE'
  | 'LISTENING'
  | 'USER_SPEAKING'
  | 'THINKING'
  | 'TOOL_USE'
  | 'CLAUDE_DEEP_REASONING'
  | 'ASSISTANT_SPEAKING'
  | 'ALERT'
  | 'ERROR'
  | 'DISCONNECTED';

export interface OrbInputs {
  /** Realtime websocket to the backend voice session is open. */
  connected: boolean;
  /** Backend VoiceState: IDLE | LISTENING | USER_SPEAKING | ENDPOINTING | THINKING | ASSISTANT_SPEAKING | INTERRUPTING | ERROR */
  backend: string;
  /** Backend voice provider: GEMINI_LIVE | LOCAL_STREAMING | '' (unknown yet). */
  provider: string;
  /** Gemini Live session status from provider_status (IDLE | CONNECTING | CONNECTED | ERROR). */
  gemini: string;
  /** A backend tool is running: the tool name, or null. */
  tool: string | null;
  /** The user clicked the orb and asked TARS to listen (visual attention window end, ms epoch). */
  attentionUntil: number;
  /** Transient alert pulse end (ms epoch). */
  alertUntil: number;
  now: number;
}

export const initialInputs = (): OrbInputs => ({
  connected: false, backend: 'IDLE', provider: '', gemini: 'IDLE', tool: null,
  attentionUntil: 0, alertUntil: 0, now: Date.now(),
});

export function deriveOrbState(i: OrbInputs): OrbState {
  if (!i.connected) return 'DISCONNECTED';
  if (i.backend === 'ERROR' || (i.provider === 'GEMINI_LIVE' && i.gemini === 'ERROR')) return 'ERROR';
  // Alert is a short pulse; it never masks the user speaking or TARS speaking.
  if (i.alertUntil > i.now && i.backend !== 'USER_SPEAKING' && i.backend !== 'ASSISTANT_SPEAKING') return 'ALERT';
  switch (i.backend) {
    case 'USER_SPEAKING':
    case 'ENDPOINTING':
      return 'USER_SPEAKING';
    case 'ASSISTANT_SPEAKING':
      return 'ASSISTANT_SPEAKING';
    case 'INTERRUPTING':
      return 'USER_SPEAKING';
    default:
      break;
  }
  if (i.tool) return i.tool === 'ask_claude' ? 'CLAUDE_DEEP_REASONING' : 'TOOL_USE';
  if (i.backend === 'THINKING') return 'THINKING';
  // LISTENING is only "awake" when a conversation is actually live: an open Gemini session, or the user
  // just asked TARS to listen. The always-on local mic gate alone is calm idle.
  if (i.backend === 'LISTENING') {
    const live = i.provider === 'GEMINI_LIVE' ? i.gemini === 'CONNECTED' : false;
    if (live || i.attentionUntil > i.now) return 'LISTENING';
  }
  return 'IDLE';
}

/** Which audio energy drives the orb / waveform in a state. */
export function levelSource(state: OrbState): 'mic' | 'assistant' | 'none' {
  if (state === 'USER_SPEAKING' || state === 'LISTENING') return 'mic';
  if (state === 'ASSISTANT_SPEAKING') return 'assistant';
  return 'none';
}

export const WAVEFORM_STATES: ReadonlySet<OrbState> = new Set<OrbState>(['LISTENING', 'USER_SPEAKING', 'ASSISTANT_SPEAKING']);
