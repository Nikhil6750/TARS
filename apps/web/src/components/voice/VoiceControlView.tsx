import React, { useState } from 'react';
import {
  Mic,
  Volume2,
  Shield,
  Info,
  CheckCircle2,
  AlertTriangle
} from 'lucide-react';
import { audioService } from '../../services/audio';

interface VoiceControlViewProps {
  isListening: boolean;
  onTogglePushToTalk: () => void;
  audioVolume: number;
  onVoiceTranscribed?: (text: string) => void;
}

export const VoiceControlView: React.FC<VoiceControlViewProps> = ({
  isListening,
  onTogglePushToTalk,
  audioVolume
}) => {
  const [hasMicPermission, setHasMicPermission] = useState<boolean | null>(null);

  const handleCheckPermission = async () => {
    const granted = await audioService.requestMicrophonePermission();
    setHasMicPermission(granted);
  };

  const handleTestTTS = () => {
    audioService.speakText('TARS voice synthesizer online. All quantitative risk parameters nominal.');
  };

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
      <div className="my-8 flex flex-col items-center justify-center">
        <div className="relative flex items-center justify-center">
          {/* Animated Glowing Outer Ring */}
          <div
            className={`absolute w-52 h-52 rounded-full transition-all duration-300 pointer-events-none ${
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
            className="absolute w-40 h-40 rounded-full border border-cyan-400/40 pointer-events-none transition-all"
            style={{
              transform: `scale(${1 + (isListening ? audioVolume * 0.6 : 0)})`,
              opacity: isListening ? 0.8 : 0.3
            }}
          />

          {/* Central PTT Button */}
          <button
            onClick={onTogglePushToTalk}
            className={`w-32 h-32 rounded-full flex flex-col items-center justify-center font-mono font-bold transition-all z-10 cursor-pointer ${
              isListening
                ? 'bg-gradient-to-br from-emerald-400 to-emerald-600 text-slate-950 shadow-[0_0_30px_rgba(0,255,102,0.8)]'
                : 'bg-gradient-to-br from-[#0c1c36] to-[#060e1d] hover:from-cyan-900/60 hover:to-cyan-950/80 text-cyan-300 border-2 border-cyan-500/50 shadow-[0_0_20px_rgba(0,240,255,0.2)]'
            }`}
          >
            {isListening ? (
              <>
                <Mic className="w-8 h-8 animate-bounce mb-1" />
                <span className="text-[11px] tracking-wider">LISTENING</span>
              </>
            ) : (
              <>
                <Mic className="w-8 h-8 mb-1" />
                <span className="text-[11px] tracking-wider">PUSH TO TALK</span>
              </>
            )}
          </button>
        </div>

        {/* Real-time Audio Level Bar */}
        <div className="mt-8 w-64 flex flex-col items-center gap-1.5">
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

      {/* Voice Provider Diagnostic Cards */}
      <div className="w-full grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-3 my-2 text-xs font-mono">
        <div className="p-2.5 rounded-lg bg-[#050b14] border border-slate-800">
          <span className="text-[10px] text-slate-500 block">WAKE WORD (LAPTOP)</span>
          <span className="font-bold text-slate-200">openWakeWord</span>
          <span className="text-[10px] text-cyan-400/80 block">Phrase: &quot;TARS&quot;</span>
        </div>
        <div className="p-2.5 rounded-lg bg-[#050b14] border border-slate-800">
          <span className="text-[10px] text-slate-500 block">VAD ENGINE</span>
          <span className="font-bold text-slate-200">Silero VAD</span>
          <span className="text-[10px] text-emerald-400/80 block">Local Free Stack</span>
        </div>
        <div className="p-2.5 rounded-lg bg-[#050b14] border border-slate-800">
          <span className="text-[10px] text-slate-500 block">STT PROVIDER</span>
          <span className="font-bold text-slate-200">faster-whisper</span>
          <span className="text-[10px] text-emerald-400/80 block">Zero Cloud Fees</span>
        </div>
        <div className="p-2.5 rounded-lg bg-[#050b14] border border-slate-800">
          <span className="text-[10px] text-slate-500 block">TTS SYNTHESIS</span>
          <span className="font-bold text-slate-200">Fish Speech / Kokoro</span>
          <span className="text-[10px] text-emerald-400/80 block">Local Neural Fallback</span>
        </div>
      </div>

      {/* Mobile / iOS Architectural Notice (ADR-007) */}
      <div className="w-full mt-3 p-3 rounded-lg bg-[#091526] border border-cyan-500/20 text-xs font-mono text-slate-300 flex items-start gap-2.5">
        <Info className="w-4 h-4 text-cyan-400 shrink-0 mt-0.5" />
        <div className="text-[11px] leading-relaxed">
          <span className="font-bold text-cyan-300">PWA / iOS Voice Architecture Notice (ADR-007): </span>
          Per iOS platform security sandbox restrictions, continuous background wake-word listening is not supported when the PWA is backgrounded or locked.
          Push-to-Talk and foreground conversational voice are active and guaranteed.
        </div>
      </div>

      {/* Actions */}
      <div className="w-full pt-4 border-t border-slate-800 flex flex-wrap items-center justify-between gap-2">
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
