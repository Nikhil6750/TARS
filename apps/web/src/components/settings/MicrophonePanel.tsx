import React, { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { MicInfo, MicTestResult, SILENT_DB, judgeMicTest, meterFraction, micStore } from '../../orb/micStore';
import { isTauri } from '../../services/tauri';
import { muteStore } from '../../orb/muteStore';

const PREF_KEY = 'tars.mic.preferred';
const API = 'http://127.0.0.1:8000';

interface DeviceRow { name: string; is_default: boolean }
interface Diagnostics {
  mic?: { frames: number; db: number; max_db: number; vad: boolean; sent_to_gemini: number; health: string };
  gemini?: string;
  transcript?: string;
  voice_state?: string;
  voice_provider?: string;
}

async function call<T>(cmd: string, args?: Record<string, unknown>): Promise<T | undefined> {
  if (!isTauri()) return undefined;
  try {
    const { invoke } = await import('@tauri-apps/api/core');
    return await invoke<T>(cmd, args);
  } catch {
    return undefined;
  }
}

/** Apply the saved microphone preference to the native capture (called once at startup too). */
export async function applySavedMicrophone(): Promise<void> {
  let saved = '';
  try { saved = localStorage.getItem(PREF_KEY) ?? ''; } catch { /* storage unavailable */ }
  if (saved) await call('set_preferred_mic', { name: saved });
}

const Meter: React.FC<{ db: number }> = ({ db }) => {
  const filled = Math.round(meterFraction(db) * 20);
  return (
    <div role="meter" aria-label="Microphone level" aria-valuemin={-120} aria-valuemax={0} aria-valuenow={Math.round(db)}
      className="flex gap-[2px]" data-testid="mic-meter">
      {Array.from({ length: 20 }, (_, i) => (
        <span key={i} className={`h-3 w-2 rounded-sm ${i < filled ? (i > 15 ? 'bg-amber-500' : 'bg-emerald-500') : 'bg-slate-200'}`} />
      ))}
    </div>
  );
};

export const MicrophonePanel: React.FC = () => {
  const info: MicInfo | null = useSyncExternalStore(micStore.subscribe, micStore.getSnapshot);
  const muted = useSyncExternalStore(muteStore.subscribe, muteStore.getSnapshot);
  const [devices, setDevices] = useState<DeviceRow[]>([]);
  const [preferred, setPreferred] = useState<string>(() => {
    try { return localStorage.getItem(PREF_KEY) ?? ''; } catch { return ''; }
  });
  const [test, setTest] = useState<{ running: boolean; result: MicTestResult | null; left: number }>({ running: false, result: null, left: 0 });
  const [human, setHuman] = useState(false);
  const [diag, setDiag] = useState<Diagnostics | null>(null);
  const peak = useRef(-120);
  const frames0 = useRef(0);
  const infoRef = useRef(info);
  infoRef.current = info;

  const refreshDevices = useCallback(async () => {
    setDevices((await call<DeviceRow[]>('list_input_devices')) ?? []);
  }, []);
  useEffect(() => { void refreshDevices(); }, [refreshDevices]);

  const choose = async (name: string) => {
    setPreferred(name);
    try { localStorage.setItem(PREF_KEY, name); } catch { /* storage unavailable */ }
    await call('set_preferred_mic', { name });
  };

  // Microphone self-test: native levels only. It never touches Gemini and costs no quota.
  const runTest = useCallback(() => {
    peak.current = -120;
    frames0.current = infoRef.current?.frames_total ?? 0;
    setTest({ running: true, result: null, left: 5 });
    const started = Date.now();
    const timer = window.setInterval(() => {
      const cur = infoRef.current;
      if (cur) peak.current = Math.max(peak.current, cur.rms_db);
      const left = Math.max(0, 5 - (Date.now() - started) / 1000);
      if (left <= 0) {
        window.clearInterval(timer);
        const seen = (infoRef.current?.frames_total ?? 0) - frames0.current;
        setTest({ running: false, result: judgeMicTest(peak.current, seen, infoRef.current?.device ?? 'no device'), left: 0 });
      } else {
        setTest(t => ({ ...t, left }));
      }
    }, 100);
  }, []);

  // Tray "Microphone Test": Settings opens and the test starts.
  useEffect(() => {
    const go = () => {
      try {
        if (sessionStorage.getItem('tars.micTest.pending')) {
          sessionStorage.removeItem('tars.micTest.pending');
          runTest();
        }
      } catch { /* storage unavailable */ }
    };
    go();
    window.addEventListener('tars-mic-test', go);
    return () => window.removeEventListener('tars-mic-test', go);
  }, [runTest]);

  // Human test mode: live view of the whole path (mic -> local VAD -> Gemini -> transcript -> state).
  useEffect(() => {
    if (!human) return;
    let alive = true;
    const tick = async () => {
      try {
        const r = await fetch(`${API}/api/v1/voice/realtime/diagnostics`);
        if (alive && r.ok) setDiag((await r.json()) as Diagnostics);
      } catch { if (alive) setDiag(null); }
    };
    void tick();
    const t = window.setInterval(tick, 400);
    return () => { alive = false; window.clearInterval(t); };
  }, [human]);

  const silent = info && info.status === 'OPEN' && info.silent_for_s > 20;
  const level = info?.status === 'OPEN' ? info.rms_db : -120;

  return (
    <section aria-label="Microphone" className="mb-6 rounded-lg border border-slate-200 p-4 text-slate-800">
      <h3 className="text-sm font-semibold mb-3">Microphone</h3>
      <div className="grid gap-3 text-[13px]">
        <label className="flex items-center gap-2">
          <span className="w-28 text-slate-500">Input device</span>
          <select aria-label="Microphone device" value={preferred} onChange={e => void choose(e.target.value)}
            className="flex-1 rounded border border-slate-300 bg-white px-2 py-1">
            <option value="">Automatic (Windows default)</option>
            {devices.map(d => <option key={d.name} value={d.name}>{d.name}{d.is_default ? ' (default)' : ''}</option>)}
          </select>
          <button type="button" onClick={() => void refreshDevices()} className="rounded border border-slate-300 px-2 py-1 hover:bg-slate-50">Refresh</button>
        </label>

        <label className="flex items-center gap-2">
          <span className="w-28 text-slate-500">Mute</span>
          <input type="checkbox" role="switch" aria-label="Mute microphone" checked={muted}
            onChange={e => void muteStore.set(e.target.checked)} data-testid="settings-mute" />
          <span>{muted ? 'Microphone muted: TARS is not listening (monitors keep running)' : 'Microphone active'}</span>
        </label>
        <div className="flex items-center gap-2">
          <span className="w-28 text-slate-500">In use</span>
          <span data-testid="mic-device">{info?.device ? `${info.device} · ${info.sample_rate} Hz · ${info.channels} ch · ${info.selected_by}${info.status === 'STARTING' ? ' · waiting for first audio' : ''}` : info?.error ?? 'No microphone signal path'}</span>
        </div>
        <div className="flex items-center gap-3">
          <span className="w-28 text-slate-500">Level</span>
          <Meter db={level} />
          <span className="tabular-nums text-slate-500">{info?.status === 'OPEN' ? `${info.rms_db.toFixed(0)} dB · ${info.frames_per_sec.toFixed(0)} fps` : '—'}</span>
        </div>
        {silent && (
          <div role="status" className="rounded bg-amber-50 px-3 py-2 text-amber-900" data-testid="mic-silent-note">
            The selected microphone has been digitally silent for {Math.round(info!.silent_for_s)} s (below {SILENT_DB} dB).
            {devices.length > 1 ? ' Another input device is available: choose it above and run the test.' : ''}
          </div>
        )}

        <div className="flex items-center gap-3">
          <button type="button" onClick={runTest} disabled={test.running}
            className="rounded bg-slate-900 px-3 py-1.5 text-white disabled:opacity-50">
            {test.running ? `Listening… ${Math.ceil(test.left)}s (say something)` : 'Test microphone'}
          </button>
          {test.result && (
            <span data-testid="mic-test-result" className={test.result.verdict === 'PASS' ? 'text-emerald-700' : 'text-rose-700'}>
              {test.result.verdict} — {test.result.device} (peak {test.result.peakDb.toFixed(0)} dB)
            </span>
          )}
        </div>

        <div className="border-t border-slate-200 pt-3">
          <button type="button" onClick={() => setHuman(h => !h)} className="rounded border border-slate-300 px-3 py-1.5 hover:bg-slate-50">
            {human ? 'Stop voice diagnostic' : 'Voice diagnostic (human test)'}
          </button>
          {human && (
            <dl className="mt-3 grid grid-cols-[9rem_1fr] gap-y-1 tabular-nums" data-testid="human-test">
              <dt className="text-slate-500">MIC DEVICE</dt><dd>{info?.device ?? '—'}</dd>
              <dt className="text-slate-500">MIC LEVEL</dt><dd><Meter db={level} /></dd>
              <dt className="text-slate-500">LOCAL VAD</dt><dd>{diag?.mic ? (diag.mic.vad ? 'SPEECH' : 'SILENCE') : '—'}</dd>
              <dt className="text-slate-500">FRAMES → GEMINI</dt><dd>{diag?.mic ? `${diag.mic.frames} received / ${diag.mic.sent_to_gemini} sent` : '—'}</dd>
              <dt className="text-slate-500">GEMINI</dt><dd>{diag?.gemini === 'CONNECTED' ? 'CONNECTED' : diag?.gemini === 'CONNECTING' ? 'CONNECTING' : diag ? 'DISCONNECTED' : '—'}</dd>
              <dt className="text-slate-500">INPUT TRANSCRIPT</dt><dd>{diag?.transcript || '—'}</dd>
              <dt className="text-slate-500">VOICE STATE</dt><dd>{diag?.voice_state ?? '—'}{diag?.mic ? ` · mic ${diag.mic.health}` : ''}</dd>
              <dd className="col-span-2 text-slate-500 mt-1">Say: “TARS, can you hear me?” — the meter moves, VAD shows SPEECH, Gemini connects, your words appear, TARS answers.</dd>
            </dl>
          )}
        </div>
      </div>
    </section>
  );
};
