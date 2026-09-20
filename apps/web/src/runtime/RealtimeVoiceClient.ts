import { audioService } from '../services/audio';
import { loadSettings } from '../services/storage';
import { isTauri } from '../services/tauri';

export interface VoiceEvent {
  type: string;
  turn_id?: string;
  generation?: number;
  previous_turn_id?: string;
  state?: string;
  text?: string;
  audio?: string;
  sequence?: number;
  response?: { display_text: string };
  providers?: Record<string, string>;
  detail?: string;
}

/** Audio transport/renderer only. The server owns every conversational state. */
export class RealtimeVoiceClient {
  private socket: WebSocket | null = null;
  private unlisten: Array<() => void> = [];
  private retry: ReturnType<typeof setTimeout> | null = null;
  private stopped = true;
  private lifecycle = 0;
  private epoch = 0;
  private generation = -1;
  private queue: VoiceEvent[] = [];
  private playing = false;
  private activeTurn = '';
  private listener: (event: VoiceEvent) => void = () => {};

  async start(listener: (event: VoiceEvent) => void, onLevel: (level: number) => void) {
    if (!isTauri() || !this.stopped) return;
    this.stopped = false;
    const lifecycle = ++this.lifecycle;
    this.listener = listener;
    const { listen } = await import('@tauri-apps/api/event');
    if (this.stopped || lifecycle !== this.lifecycle) return;
    const handles = await Promise.all([
      listen<number[]>('tars://microphone-pcm', ({ payload }) => {
        const ws = this.socket;
        if (ws?.readyState !== WebSocket.OPEN) return;
        if (ws.bufferedAmount > 32000) { ws.close(); return; }
        const bytes = new ArrayBuffer(payload.length * 2);
        const view = new DataView(bytes);
        payload.forEach((value, i) => view.setInt16(i * 2, value, true));
        ws.send(bytes);
      }),
      listen<number>('tars://wake-audio-level', ({ payload }) => onLevel(payload)),
      listen<string>('tars://microphone-status', ({ payload }) => {
        listener({ type: 'provider_status', providers: { microphone: payload } });
        if (payload !== 'CONNECTED') this.socket?.close();
      }),
    ]);
    if (this.stopped || lifecycle !== this.lifecycle) { handles.forEach(fn => fn()); return; }
    this.unlisten = handles;
    this.connect();
  }

  private connect() {
    if (this.stopped) return;
    const url = loadSettings().apiEndpoint.replace(/^http/, 'ws').replace(/\/$/, '');
    const socket = new WebSocket(`${url}/api/v1/voice/realtime`);
    this.socket = socket;
    this.generation = -1;
    socket.onmessage = ({ data }) => {
      if (this.socket !== socket || this.stopped) return;
      try { this.accept(JSON.parse(String(data)) as VoiceEvent); }
      catch (error) { console.warn('[realtime] invalid event', error); }
    };
    socket.onclose = () => {
      if (this.socket !== socket) return;
      this.flush();
      this.listener({ type: 'state', state: 'ERROR', detail: 'Voice disconnected; reconnecting' });
      if (!this.stopped) this.retry = setTimeout(() => this.connect(), 1500);
    };
    socket.onerror = () => socket.close();
  }

  // Public for the deterministic transport race harness.
  accept(event: VoiceEvent) {
    if (event.type === 'interrupt') {
      if ((event.generation ?? -1) < this.generation) return;
      this.generation = event.generation ?? this.generation;
      this.activeTurn = event.turn_id ?? '';
      this.flush();
      this.send({ type: 'playback_stopped', turn_id: event.previous_turn_id });
    } else if (event.generation !== undefined) {
      if (event.generation < this.generation) return;
      this.generation = event.generation;
      this.activeTurn = event.turn_id ?? this.activeTurn;
    }
    this.listener(event);
    if (event.type === 'audio_chunk' && event.audio) {
      if (this.queue.length >= 4) { this.socket?.close(); this.flush(); return; }
      this.queue.push(event);
      void this.drain();
    }
  }

  private send(event: object) {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(event));
  }

  private async drain() {
    if (this.playing) return;
    this.playing = true;
    const epoch = this.epoch;
    try {
      while (this.queue.length && epoch === this.epoch) {
        const item = this.queue.shift()!;
        if (item.turn_id !== this.activeTurn) continue;
        const bytes = Uint8Array.from(atob(item.audio!), char => char.charCodeAt(0));
        const ack = (type: string) => {
          if (epoch === this.epoch) this.send({ type, turn_id: item.turn_id, sequence: item.sequence });
        };
        try {
          await audioService.playAudioBytes(bytes.buffer, undefined, () => ack('audio_started'));
          ack('audio_done');
        } catch { ack('audio_error'); }
      }
    } finally {
      if (epoch === this.epoch) this.playing = false;
    }
  }

  private flush() {
    this.epoch++;
    this.queue = [];
    this.playing = false;
    audioService.stopSpeaking();
  }

  stop() {
    this.stopped = true;
    this.lifecycle++;
    this.unlisten.forEach(fn => fn());
    this.unlisten = [];
    if (this.retry) clearTimeout(this.retry);
    this.socket?.close();
    this.socket = null;
    this.flush();
  }

  interrupt() {
    this.flush();
    this.send({ type: 'interrupt' });
  }
}

export const realtimeVoiceClient = new RealtimeVoiceClient();
