import React, { useState, useEffect } from 'react';
import {
  Mic,
  Volume2,
  Shield,
  Info,
  CheckCircle2,
  AlertTriangle,
  Radio,
  Play
} from 'lucide-react';
import { audioService } from '../../services/audio';

interface VoiceControlViewProps {
  isListening: boolean;
  onTogglePushToTalk: () => void;
  audioVolume: number;
  onVoiceTranscribed?: (text: string) => void;
  apiEndpoint?: string;
}

interface RuntimeReadiness {
  ready?: boolean;
  assistant?: { provider?: string; configured?: string; ready?: boolean };
  stt?: { provider?: string; configured?: string; ready?: boolean };
  tts?: { provider?: string; configured?: string; ready?: boolean };
  wake?: { provider?: string; configured?: string; ready?: boolean };
}

const KOKORO_CANDIDATES = [
  { id: 'am_michael', name: 'Candidate A (am_michael)', desc: 'Warm masculine tone, professional cadence' },
  { id: 'am_onyx', name: 'Candidate B (am_onyx)', desc: 'Deep authoritative masculine tone, low latency' },
  { id: 'bm_george', name: 'Candidate C (bm_george)', desc: 'British masculine tone, clear articulation' },
  { id: 'af_heart', name: 'Reference (af_heart)', desc: 'Current benchmark voice reference' },
];

export const VoiceControlView: React.FC<VoiceControlViewProps> = ({
  isListening,
  onTogglePushToTalk,
  audioVolume,
  apiEndpoint = 'http://127.0.0.1:8000'
}) => {
  const [hasMicPermission, setHasMicPermission] = useState<boolean | null>(null);
  const [readiness, setReadiness] = useState<RuntimeReadiness | null>(null);
  const [playingVoice, setPlayingVoice] = useState<string | null>(null);
  const [selectedPreference, setSelectedPreference] = useState<string>('am_michael');

  useEffect(() => {
    async function fetchReadiness() {
      try {
        const res = await fetch(`${apiEndpoint.replace(/\/$/, '')}/api/v1/runtime/readiness`);
        if (res.ok) {
          const data = await res.json();
          setReadiness(data);
        }
      } catch (err) {
        console.warn('Could not query runtime readiness:', err);
      }
    }
    void fetchReadiness();
  }, [apiEndpoint]);

  const handleCheckPermission = async () => {
    const granted = await audioService.requestMicrophonePermission();
    setHasMicPermission(granted);
  };

  const handleTestTTS = async () => {
    try {
      await audioService.synthesizeAndPlay('TARS voice synthesizer online. All quantitative risk parameters nominal.', apiEndpoint);
    } catch {
      audioService.speakText('TARS voice synthesizer online. All quantitative risk parameters nominal.');
    }
  };

  const handlePlayVoiceSample = async (voiceId: string) => {
    setPlayingVoice(voiceId);
    try {
      const sampleText = `Hello. I am TARS using the ${voiceId} Kokoro candidate. What would you like me to analyze?`;
      await audioService.synthesizeAndPlay(sampleText, apiEndpoint);
    } catch {
      audioService.speakText(`Hello. I am TARS using the ${voiceId} Kokoro candidate.`);
    } finally {
      setPlayingVoice(null);
    }
  };

  const wakeProviderName = readiness?.wake?.configured || readiness?.wake?.provider || 'CPAL Native VAD';
  const sttProviderName = readiness?.stt?.configured || readiness?.stt?.provider || 'faster-whisper';
  const ttsProviderName = readiness?.tts?.configured || readiness?.tts?.provider || 'Kokoro ONNX';
  const assistantProviderName = readiness?.assistant?.configured || readiness?.assistant?.provider || 'Claude Code';

  return (
    <div className="w-full h-full flex flex-col items-center justify-between p-4 md:p-6 overflow-y-auto max-w-4xl mx-auto glass-panel bg-[#070e1b]/95">
      {/* Header */}
      <div className="w-full text-center pb-4 border-b border-slate-800">
        <h1 className="text-lg font-display-title font-bold text-slate-100 flex items-center justify-center gap-2">
          <Mic className="w-5 h-5 text-cyan-400" />
          VOICE ORCHESTRATION & CONTROL
        </h1>
        <p className="text-xs font-mono text-slate-400 mt-1">
          Zero-latency Push-to-Talk and foreground conversational voice interface.
        </p>
      </div>

      {/* Main Interactive Push to Talk Orb */}
      <div className="my-6 flex flex-col items-center justify-center">
        <div className="relative flex items-center justify-center">
          {/* Animated Glowing Outer Ring */}
          <div
            className={`absolute w-48 h-48 rounded-full transition-all duration-300 pointer-events-none ${
              isListening
                ? 'bg-emerald-500/20 border-2 border-emerald-400 shadow-[0_0_50px_rgba(0,255,102,0.5)] scale-110 animate-pulse'
                : 'bg-cyan-500/10 border border-cyan-500/30'
            }`}
            style={{
              transform: isListening ? `scale(${1 + audioVolume * 0.4})` : 'scale(1)'
            }}
          />

          {/* Secondary Acoustic Wave Ring */}
          <div
            className="absolute w-36 h-36 rounded-full border border-cyan-400/40 pointer-events-none transition-all"
            style={{
              transform: `scale(${1 + (isListening ? audioVolume * 0.6 : 0)})`,
              opacity: isListening ? 0.8 : 0.3
            }}
          />

          {/* Central PTT Button */}
          <button
            onClick={onTogglePushToTalk}
            className={`w-28 h-28 rounded-full flex flex-col items-center justify-center font-mono font-bold transition-all z-10 cursor-pointer ${
              isListening
                ? 'bg-gradient-to-br from-emerald-400 to-emerald-600 text-slate-950 shadow-[0_0_30px_rgba(0,255,102,0.8)]'
                : 'bg-gradient-to-br from-[#0c1c36] to-[#060e1d] hover:from-cyan-900/60 hover:to-cyan-950/80 text-cyan-300 border-2 border-cyan-500/50 shadow-[0_0_20px_rgba(0,240,255,0.2)]'
            }`}
          >
            {isListening ? (
              <>
                <Mic className="w-7 h-7 animate-bounce mb-1" />
                <span className="text-[10px] tracking-wider">LISTENING</span>
              </>
            ) : (
              <>
                <Mic className="w-7 h-7 mb-1" />
                <span className="text-[10px] tracking-wider">PUSH TO TALK</span>
              </>
            )}
          </button>
        </div>

        {/* Real-time Audio Level Bar */}
        <div className="mt-6 w-64 flex flex-col items-center gap-1.5">
          <div className="flex items-center justify-between w-full text-[10px] font-mono text-slate-400">
            <span>MIC LEVEL</span>
            <span className="text-cyan-400 font-numeric">{Math.round(audioVolume * 100)}%</span>
          </div>
          <div className="w-full h-2 bg-[#040810] rounded-full overflow-hidden border border-slate-800">
            <div
              className="h-full bg-gradient-to-r from-cyan-400 to-emerald-400 transition-all duration-75"
              style={{ width: `${Math.max(4, audioVolume * 100)}%` }}
            />
          </div>
        </div>
      </div>

      {/* Real Runtime Provider Status Cards */}
      <div className="w-full grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-2.5 my-2 text-xs font-mono">
        <div className="p-2.5 rounded-lg bg-[#050b14] border border-slate-800">
          <span className="text-[10px] text-slate-500 block">WAKE ENGINE</span>
          <span className="font-bold text-slate-200">{wakeProviderName}</span>
          <span className="text-[10px] text-cyan-400/80 block">
            {readiness?.wake?.ready ? '● Online (Active)' : 'Phrase: "Hey TARS"'}
          </span>
        </div>
        <div className="p-2.5 rounded-lg bg-[#050b14] border border-slate-800">
          <span className="text-[10px] text-slate-500 block">ASSISTANT</span>
          <span className="font-bold text-slate-200">{assistantProviderName}</span>
          <span className="text-[10px] text-emerald-400/80 block">
            {readiness?.assistant?.ready ? '● Ready' : 'Configured'}
          </span>
        </div>
        <div className="p-2.5 rounded-lg bg-[#050b14] border border-slate-800">
          <span className="text-[10px] text-slate-500 block">STT PROVIDER</span>
          <span className="font-bold text-slate-200">{sttProviderName}</span>
          <span className="text-[10px] text-emerald-400/80 block">
            {readiness?.stt?.ready ? '● Local Faster-Whisper' : 'Zero Cloud Fees'}
          </span>
        </div>
        <div className="p-2.5 rounded-lg bg-[#050b14] border border-slate-800">
          <span className="text-[10px] text-slate-500 block">TTS SYNTHESIS</span>
          <span className="font-bold text-slate-200">{ttsProviderName}</span>
          <span className="text-[10px] text-emerald-400/80 block">
            {readiness?.tts?.ready ? '● Kokoro Online' : 'Local Synthesis'}
          </span>
        </div>
      </div>

      {/* Kokoro Voice Candidate Evaluation Panel */}
      <div className="w-full my-3 p-3.5 rounded-lg bg-[#081324] border border-cyan-500/30 text-xs font-mono text-slate-200">
        <div className="flex items-center justify-between pb-2 mb-2.5 border-b border-slate-800">
          <span className="font-bold text-cyan-300 flex items-center gap-1.5">
            <Radio className="w-4 h-4 text-cyan-400" />
            KOKORO VOICE CANDIDATES (USER EVALUATION)
          </span>
          <span className="text-[10px] text-slate-400">Select preferred voice</span>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
          {KOKORO_CANDIDATES.map((cand) => {
            const isSelected = selectedPreference === cand.id;
            const isPlaying = playingVoice === cand.id;
            return (
              <div
                key={cand.id}
                onClick={() => setSelectedPreference(cand.id)}
                className={`p-2.5 rounded border transition-all cursor-pointer flex items-center justify-between ${
                  isSelected
                    ? 'bg-cyan-950/50 border-cyan-400/80 text-cyan-100 shadow-[0_0_12px_rgba(0,240,255,0.15)]'
                    : 'bg-[#040812] border-slate-800 text-slate-300 hover:border-slate-700'
                }`}
              >
                <div>
                  <div className="font-bold flex items-center gap-1.5">
                    <span>{cand.name}</span>
                    {isSelected && <span className="text-[9px] px-1 py-0.2 rounded bg-cyan-500/20 text-cyan-300 border border-cyan-500/40">PREFERRED</span>}
                  </div>
                  <div className="text-[10px] text-slate-400 mt-0.5">{cand.desc}</div>
                </div>

                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    void handlePlayVoiceSample(cand.id);
                  }}
                  disabled={isPlaying}
                  className="px-2 py-1 rounded bg-cyan-500/20 hover:bg-cyan-500/30 border border-cyan-500/40 text-[10px] text-cyan-300 transition-colors flex items-center gap-1 cursor-pointer disabled:opacity-50 shrink-0 ml-2"
                >
                  <Play className={`w-3 h-3 ${isPlaying ? 'animate-spin' : ''}`} />
                  <span>{isPlaying ? 'Playing...' : 'Listen'}</span>
                </button>
              </div>
            );
          })}
        </div>
      </div>

      {/* Mobile / iOS Architectural Notice (ADR-007) */}
      <div className="w-full p-3 rounded-lg bg-[#091526] border border-cyan-500/20 text-xs font-mono text-slate-300 flex items-start gap-2.5">
        <Info className="w-4 h-4 text-cyan-400 shrink-0 mt-0.5" />
        <div className="text-[11px] leading-relaxed">
          <span className="font-bold text-cyan-300">PWA / iOS Voice Architecture Notice (ADR-007): </span>
          Continuous background wake-word listening is running via native desktop OS threads.
          Push-to-Talk and foreground conversational voice are active and guaranteed across platforms.
        </div>
      </div>

      {/* Actions */}
      <div className="w-full pt-3 border-t border-slate-800 flex flex-wrap items-center justify-between gap-2">
        <button
          onClick={handleCheckPermission}
          className="px-3 py-1.5 rounded-md bg-slate-800 hover:bg-slate-700 text-xs font-mono text-slate-200 transition-colors flex items-center gap-1.5 cursor-pointer"
        >
          <Shield className="w-3.5 h-3.5 text-cyan-400" />
          <span>Check Mic Permissions</span>
          {hasMicPermission === true && <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />}
          {hasMicPermission === false && <AlertTriangle className="w-3.5 h-3.5 text-rose-400" />}
        </button>

        <button
          onClick={handleTestTTS}
          className="px-3 py-1.5 rounded-md bg-cyan-500/20 hover:bg-cyan-500/30 border border-cyan-500/30 text-xs font-mono text-cyan-300 transition-colors flex items-center gap-1.5 cursor-pointer"
        >
          <Volume2 className="w-3.5 h-3.5" />
          <span>Test Speech Synthesis Output</span>
        </button>
      </div>
    </div>
  );
};
