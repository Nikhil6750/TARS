import { OrbInputs, OrbState, deriveOrbState, initialInputs } from './orbState';

export interface OrbBubble {
  text: string;
  kind: 'alert' | 'action' | 'error';
  at: number;
  replay?: boolean;
}

export interface OrbSnapshot {
  state: OrbState;
  connected: boolean;
  provider: string;
  muted: boolean;
  transcript: string;
  caption: string;
  bubble: OrbBubble | null;
  confirm: { text: string } | null;
  flashUntil: number;
  lastAlert: OrbBubble | null;
}

/** Minimal shape of the realtime events the store understands (subset of VoiceEvent). */
export interface OrbEvent {
  type: string;
  state?: string;
  text?: string;
  name?: string;
  status?: string;
  detail?: string;
  voice_provider?: string;
  connected?: boolean;
  providers?: Record<string, string>;
  response?: { display_text: string };
}

const TRANSCRIPT_MS = 2600;
const CAPTION_MS = 3200;
const BUBBLE_MS = 7000;
const ALERT_PULSE_MS = 1900;
const ATTENTION_MS = 9000;
const FLASH_MS = 1100;

const tail = (text: string, max: number) => (text.length > max ? '…' + text.slice(text.length - max + 1).trimStart() : text);

export class OrbStore {
  private inputs: OrbInputs = initialInputs();
  private muted = false;
  private transcript = '';
  private transcriptAt = 0;
  private caption = '';
  private captionAt = 0;
  private bubble: OrbBubble | null = null;
  private confirm: { text: string } | null = null;
  private flashUntil = 0;
  private lastAlert: OrbBubble | null = null;
  private listeners = new Set<() => void>();
  private timer: ReturnType<typeof setTimeout> | null = null;
  private snap: OrbSnapshot;
  private clock: () => number;

  constructor(clock: () => number = () => Date.now()) {
    this.clock = clock;
    this.snap = this.build();
  }

  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => { this.listeners.delete(fn); };
  };

  getSnapshot = (): OrbSnapshot => this.snap;

  private build(): OrbSnapshot {
    const now = this.clock();
    this.inputs.now = now;
    return {
      state: deriveOrbState(this.inputs),
      connected: this.inputs.connected,
      provider: this.inputs.provider,
      muted: this.muted,
      transcript: now - this.transcriptAt < TRANSCRIPT_MS ? this.transcript : '',
      caption: now - this.captionAt < CAPTION_MS ? this.caption : '',
      bubble: this.bubble && now - this.bubble.at < BUBBLE_MS ? this.bubble : null,
      confirm: this.confirm,
      flashUntil: this.flashUntil,
      lastAlert: this.lastAlert,
    };
  }

  private commit() {
    const next = this.build();
    const prev = this.snap;
    const same = next.state === prev.state && next.connected === prev.connected && next.provider === prev.provider
      && next.muted === prev.muted && next.transcript === prev.transcript && next.caption === prev.caption
      && next.bubble === prev.bubble && next.confirm === prev.confirm && next.flashUntil === prev.flashUntil
      && next.lastAlert === prev.lastAlert;
    if (!same) {
      this.snap = next;
      this.listeners.forEach(fn => fn());
    }
    this.scheduleExpiry();
  }

  /** One timer wakes the store when the next visual expiry is due; no polling. */
  private scheduleExpiry() {
    if (this.timer) clearTimeout(this.timer);
    const now = this.clock();
    const due = [
      this.transcript && this.transcriptAt + TRANSCRIPT_MS,
      this.caption && this.captionAt + CAPTION_MS,
      this.bubble && this.bubble.at + BUBBLE_MS,
      this.inputs.alertUntil,
      this.inputs.attentionUntil,
      this.flashUntil,
    ].filter((t): t is number => typeof t === 'number' && t > now);
    if (due.length) this.timer = setTimeout(() => this.commit(), Math.max(30, Math.min(...due) - now + 5));
  }

  // ---- inputs -------------------------------------------------------------------------------
  apply(ev: OrbEvent) {
    const now = this.clock();
    switch (ev.type) {
      case 'connection':
        this.inputs.connected = !!ev.connected;
        break;
      case 'state':
        this.inputs.backend = ev.state ?? 'IDLE';
        if (this.inputs.backend === 'ASSISTANT_SPEAKING' || this.inputs.backend === 'IDLE') this.inputs.tool = null;
        break;
      case 'provider_status':
        if (ev.voice_provider) this.inputs.provider = ev.voice_provider;
        if (ev.providers?.gemini_live) this.inputs.gemini = ev.providers.gemini_live;
        if (ev.detail && /Using LOCAL_STREAMING/i.test(ev.detail)) {
          this.setBubble({ text: 'Using local voice', kind: 'error', at: now });
        }
        break;
      case 'speech_started':
        this.transcript = '';
        this.caption = '';
        break;
      case 'partial_transcript':
      case 'final_transcript':
        this.transcript = ev.text ?? '';
        this.transcriptAt = now;
        break;
      case 'delta':
        this.caption = tail(this.caption + (ev.text ?? ''), 110);
        this.captionAt = now;
        break;
      case 'response_complete':
        this.caption = tail(ev.response?.display_text ?? this.caption, 110);
        this.captionAt = now;
        break;
      case 'tool_call':
        this.inputs.tool = ev.name ?? 'tool';
        break;
      case 'tool_result':
        this.inputs.tool = null;
        if (ev.status === 'DONE') {
          this.flashUntil = now + FLASH_MS;
        } else if (ev.status && ev.status !== 'NEEDS_CONFIRMATION') {
          this.setBubble({ text: `Couldn't do that (${ev.status.toLowerCase().replace('_', ' ')})`, kind: 'error', at: now });
        }
        break;
      case 'confirmation_pending':
        this.confirm = { text: ev.text ?? 'Confirm this action?' };
        break;
      case 'confirmation_cleared':
        this.confirm = null;
        break;
      case 'interrupt':
        this.inputs.tool = null;
        this.caption = '';
        break;
      default:
        return;
    }
    this.commit();
  }

  private setBubble(b: OrbBubble) {
    this.bubble = b;
  }

  alert(text: string, replay = false) {
    const now = this.clock();
    const b: OrbBubble = { text, kind: 'alert', at: now, replay };
    this.lastAlert = b;
    this.bubble = b;
    this.inputs.alertUntil = now + ALERT_PULSE_MS;
    this.commit();
  }

  showLastAlert() {
    if (this.lastAlert) {
      this.bubble = { ...this.lastAlert, at: this.clock() };
      this.commit();
    }
  }

  dismissBubble() {
    this.bubble = null;
    this.commit();
  }

  attend() {
    this.inputs.attentionUntil = this.clock() + ATTENTION_MS;
    this.commit();
  }

  setMuted(muted: boolean) {
    this.muted = muted;
    this.commit();
  }

  reset() {
    this.inputs = initialInputs();
    this.muted = false;
    this.transcript = this.caption = '';
    this.transcriptAt = this.captionAt = 0;
    this.bubble = this.lastAlert = null;
    this.confirm = null;
    this.flashUntil = 0;
    this.commit();
  }
}

export const orbStore = new OrbStore();
