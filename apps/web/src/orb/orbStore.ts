import { OrbInputs, OrbState, deriveOrbState, initialInputs } from './orbState';

export interface OrbBubble {
  text: string;
  kind: 'alert' | 'action' | 'error';
  at: number;
  replay?: boolean;
}

export type ActivityKind = 'acting' | 'done' | 'error' | null;

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
  /** Truthful, backend-derived description of what TARS is doing right now
   * (mission: "activity text must be driven by actual execution state", see
   * voice/activity.py) -- "Opening Chrome…", "Done", "Couldn't find Calculator".
   * Null when there is nothing to show (collapses the pill back to the orb). */
  activityText: string | null;
  activityKind: ActivityKind;
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
  microphone_muted?: boolean;
  response?: { display_text: string };
}

const TRANSCRIPT_MS = 2600;
const CAPTION_MS = 3200;
const BUBBLE_MS = 7000;
const ALERT_PULSE_MS = 1900;
const ATTENTION_MS = 9000;
const FLASH_MS = 1100;
/** How long "Done"/an error line lingers before the pill collapses back to
 * the orb (mission: "remain expanded briefly (~1.5-2 sec) then collapse"). */
const ACTIVITY_SETTLE_MS = 1800;

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
  private activityText = '';
  private activityKind: ActivityKind = null;
  private activityAt = 0;
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
    // "acting" has no timeout -- it stays until the tool actually finishes
    // (multi-step tasks can run long); "done"/"error" linger briefly then
    // collapse the pill back to the orb.
    const activityLive = this.activityKind === 'acting' || (this.activityKind !== null && now - this.activityAt < ACTIVITY_SETTLE_MS);
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
      activityText: activityLive ? this.activityText : null,
      activityKind: activityLive ? this.activityKind : null,
    };
  }

  private commit() {
    const next = this.build();
    const prev = this.snap;
    const same = next.state === prev.state && next.connected === prev.connected && next.provider === prev.provider
      && next.muted === prev.muted && next.transcript === prev.transcript && next.caption === prev.caption
      && next.bubble === prev.bubble && next.confirm === prev.confirm && next.flashUntil === prev.flashUntil
      && next.lastAlert === prev.lastAlert && next.activityText === prev.activityText && next.activityKind === prev.activityKind;
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
      this.activityKind && this.activityKind !== 'acting' && this.activityAt + ACTIVITY_SETTLE_MS,
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
        if (ev.providers?.microphone) this.inputs.mic = ev.providers.microphone;
        if (typeof ev.microphone_muted === 'boolean') this.muted = ev.microphone_muted;
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
        // Always overwrites, even mid-settle of a previous "Done"/error --
        // a multi-step task's next real action replaces that immediately
        // rather than waiting out the linger (mission: update the pill as
        // each real action begins, never show a stale state).
        this.activityText = ev.text ?? '';
        this.activityKind = 'acting';
        this.activityAt = now;
        break;
      case 'tool_result':
        this.inputs.tool = null;
        if (ev.status === 'DONE') {
          this.flashUntil = now + FLASH_MS;
          this.activityKind = 'done';
        } else if (ev.status && ev.status !== 'NEEDS_CONFIRMATION') {
          this.activityKind = 'error';
        } else {
          this.activityKind = null; // NEEDS_CONFIRMATION: the confirm card speaks for this, not the pill
        }
        this.activityText = ev.text ?? this.activityText;
        this.activityAt = now;
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
        this.activityText = '';
        this.activityKind = null;
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
    const now = this.clock();
    if (this.inputs.mic === 'SILENT' || this.inputs.mic === 'DISCONNECTED') {
      this.setBubble({ text: this.inputs.mic === 'SILENT' ? 'Microphone is silent. Check Settings.' : 'No microphone signal. Check Settings.', kind: 'error', at: now });
    }
    this.inputs.attentionUntil = now + ATTENTION_MS;
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
    this.activityText = '';
    this.activityKind = null;
    this.activityAt = 0;
    this.commit();
  }
}

export const orbStore = new OrbStore();
