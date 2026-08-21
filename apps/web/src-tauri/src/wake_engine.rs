//! Native background wake-word runtime.
//!
//! Owns the microphone directly via `cpal` on its own OS thread so "Hey
//! TARS" detection is independent of the React panel's lifecycle: hiding,
//! closing, or never having opened the panel does not stop this engine --
//! only process exit (tray Quit) does. The panel only ever renders state
//! pushed to it through Tauri events; it never touches audio itself.
//!
//! Ports the energy-based VAD + local-whisper-transcribe + regex wake
//! match design into Rust with explicit state machine transitions and
//! latency instrumentation.
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use regex::Regex;
use serde::{Deserialize, Serialize};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, RecvTimeoutError};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Emitter};

const FRAME_SIZE: usize = 2048;
const CALIBRATION_FRAMES: u32 = 12;
const NOISE_FLOOR_MULTIPLIER: f32 = 2.5;
const MIN_THRESHOLD: f32 = 0.012;
const SILENCE_HANG_MS: f32 = 700.0;
const MIN_UTTERANCE_MS: f32 = 250.0;
const MAX_UTTERANCE_MS: f32 = 9000.0;
const PRE_ROLL_FRAMES: usize = 4;
const COMMAND_TIMEOUT_MS: u64 = 7000;
const WAKE_COOLDOWN_MS: u64 = 2000;

#[derive(Clone, Copy, PartialEq, Eq, Debug, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum WakeState {
    Idle,
    Audio,
    Transcribing,
    WakeDetected,
    CommandListening,
    Processing,
    Speaking,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct WakeTimingTelemetry {
    pub state: WakeState,
    pub audio_detected_at: Option<u64>,
    pub speech_end_at: Option<u64>,
    pub transcription_start: Option<u64>,
    pub transcription_complete: Option<u64>,
    pub wake_detected_at: Option<u64>,
    pub command_ready_at: Option<u64>,
    pub duration_ms: Option<f32>,
    pub transcript: Option<String>,
    #[serde(default)]
    pub telemetry_id: Option<String>,
}

struct SharedState {
    state: Mutex<WakeState>,
    command_deadline: Mutex<Option<Instant>>,
    last_wake_at: Mutex<Option<Instant>>,
    last_chart_at: Mutex<Option<Instant>>,
}

impl SharedState {
    fn new() -> Self {
        Self {
            state: Mutex::new(WakeState::Idle),
            command_deadline: Mutex::new(None),
            last_wake_at: Mutex::new(None),
            last_chart_at: Mutex::new(None),
        }
    }
}

static RUNNING: AtomicBool = AtomicBool::new(false);
static LAST_ERROR: Mutex<Option<String>> = Mutex::new(None);
static SHARED: OnceLock<Arc<SharedState>> = OnceLock::new();

fn shared() -> Arc<SharedState> {
    SHARED
        .get_or_init(|| Arc::new(SharedState::new()))
        .clone()
}

fn epoch_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

fn backend_base_url() -> String {
    std::env::var("TARS_BACKEND_URL").unwrap_or_else(|_| "http://127.0.0.1:8000".to_string())
}

fn wake_regex() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?i)\b(hey[\s,]+tars|tars|hey[\s,]+tar|ok[\s,]+tars|hey[\s,]+torres|hi[\s,]+tars)\b")
            .expect("valid wake regex")
    })
}

fn analyze_chart_regex() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r"(?i)\b(analy[sz]e|check|look\s+at|evaluate|read|scan|inspect|review|what\s+do\s+you\s+see\s+on)[\s,]+(?:this|the|my|active|current)?\s*charts?\b",
        )
        .expect("valid analyze-chart regex")
    })
}

#[derive(Serialize, Clone)]
struct TextPayload {
    text: String,
}

/// Starts the background wake engine on its own OS thread. Safe to call
/// once at app startup; a second call is a no-op if already running.
pub fn start(app: AppHandle) {
    if RUNNING.swap(true, Ordering::SeqCst) {
        return;
    }
    std::thread::spawn(move || {
        if let Err(err) = run(app) {
            eprintln!("[wake_engine] stopped: {err}");
            *LAST_ERROR.lock().unwrap() = Some(err);
            RUNNING.store(false, Ordering::SeqCst);
        }
    });
}

pub fn is_running() -> bool {
    RUNNING.load(Ordering::SeqCst)
}

pub fn last_error() -> Option<String> {
    LAST_ERROR.lock().unwrap().clone()
}

pub fn current_state() -> WakeState {
    *shared().state.lock().unwrap()
}

/// Forces the engine into one-shot command-capture mode immediately --
/// used for barge-in: the moment the frontend hears `speech-start` while
/// TARS is speaking, it calls this so the utterance already forming is
/// captured as the user's next command instead of being wake-word-filtered.
pub fn force_command_capture(app: Option<&AppHandle>) {
    let state = shared();
    *state.state.lock().unwrap() = WakeState::CommandListening;
    *state.command_deadline.lock().unwrap() =
        Some(Instant::now() + Duration::from_millis(COMMAND_TIMEOUT_MS));
    if let Some(app) = app {
        let _ = app.emit(
            "tars://wake-state-changed",
            WakeTimingTelemetry {
                state: WakeState::CommandListening,
                audio_detected_at: None,
                speech_end_at: None,
                transcription_start: None,
                transcription_complete: None,
                wake_detected_at: None,
                command_ready_at: None,
                duration_ms: None,
                transcript: None,
                telemetry_id: None,
            },
        );
    }
}

pub fn set_playback_speaking(speaking: bool, app: &AppHandle) {
    let state = shared();
    let next_state = if speaking {
        WakeState::Speaking
    } else {
        WakeState::Idle
    };
    *state.state.lock().unwrap() = next_state;
    let _ = app.emit(
        "tars://wake-state-changed",
        WakeTimingTelemetry {
            state: next_state,
            audio_detected_at: None,
            speech_end_at: None,
            transcription_start: None,
            transcription_complete: None,
            wake_detected_at: None,
            command_ready_at: None,
            duration_ms: None,
            transcript: None,
            telemetry_id: None,
        },
    );
}

fn run(app: AppHandle) -> Result<(), String> {
    let host = cpal::default_host();
    let device = host
        .default_input_device()
        .ok_or_else(|| "no default input (microphone) device found".to_string())?;
    let supported = device
        .default_input_config()
        .map_err(|e| format!("no usable input config: {e}"))?;
    let sample_format = supported.sample_format();
    let config: cpal::StreamConfig = supported.into();
    let sample_rate = config.sample_rate;
    let channels = config.channels as usize;

    let (tx, rx) = channel::<Vec<f32>>();

    let stream = match sample_format {
        cpal::SampleFormat::F32 => {
            let tx = tx.clone();
            device.build_input_stream(
                config.clone(),
                move |data: &[f32], _: &cpal::InputCallbackInfo| {
                    let _ = tx.send(downmix_f32(data, channels));
                },
                |err| eprintln!("[wake_engine] audio stream error: {err}"),
                None,
            )
        }
        cpal::SampleFormat::I16 => {
            let tx = tx.clone();
            device.build_input_stream(
                config.clone(),
                move |data: &[i16], _: &cpal::InputCallbackInfo| {
                    let _ = tx.send(downmix_i16(data, channels));
                },
                |err| eprintln!("[wake_engine] audio stream error: {err}"),
                None,
            )
        }
        other => return Err(format!("unsupported input sample format: {other:?}")),
    }
    .map_err(|e| format!("failed to build input stream: {e}"))?;

    stream
        .play()
        .map_err(|e| format!("failed to start microphone stream: {e}"))?;

    eprintln!("[wake_engine] listening: {sample_rate} Hz, {channels} channel(s)");

    let state = shared();
    let mut vad = VadState::new(sample_rate as f32);
    let base_url = backend_base_url();

    loop {
        match rx.recv_timeout(Duration::from_millis(500)) {
            Ok(chunk) => vad.process(&chunk, &app, &state, &base_url),
            Err(RecvTimeoutError::Timeout) => check_command_timeout(&app, &state),
            Err(RecvTimeoutError::Disconnected) => {
                return Err("audio input channel disconnected".into());
            }
        }
    }
}

fn downmix_f32(data: &[f32], channels: usize) -> Vec<f32> {
    if channels <= 1 {
        return data.to_vec();
    }
    data.chunks(channels)
        .map(|frame| frame.iter().sum::<f32>() / channels as f32)
        .collect()
}

fn downmix_i16(data: &[i16], channels: usize) -> Vec<f32> {
    if channels <= 1 {
        return data.iter().map(|s| *s as f32 / 32768.0).collect();
    }
    data.chunks(channels)
        .map(|frame| {
            let sum: f32 = frame.iter().map(|s| *s as f32 / 32768.0).sum();
            sum / channels as f32
        })
        .collect()
}

fn check_command_timeout(app: &AppHandle, state: &SharedState) {
    let mut deadline_guard = state.command_deadline.lock().unwrap();
    if let Some(deadline) = *deadline_guard {
        if Instant::now() >= deadline {
            *deadline_guard = None;
            drop(deadline_guard);
            *state.state.lock().unwrap() = WakeState::Idle;
            let _ = app.emit("tars://command-timeout", ());
            let _ = app.emit(
                "tars://wake-state-changed",
                WakeTimingTelemetry {
                    state: WakeState::Idle,
                    audio_detected_at: None,
                    speech_end_at: None,
                    transcription_start: None,
                    transcription_complete: None,
                    wake_detected_at: None,
                    command_ready_at: None,
                    duration_ms: None,
                    transcript: None,
                    telemetry_id: None,
                },
            );
        }
    }
}

struct VadState {
    sample_rate: f32,
    frame_duration_ms: f32,
    pending: Vec<f32>,
    noise_floor: f32,
    calibration_count: u32,
    calibration_sum: f32,
    speech_active: bool,
    speech_frames: Vec<f32>,
    pre_roll: Vec<Vec<f32>>,
    silence_streak_ms: f32,
    speech_duration_ms: f32,
    audio_detected_at: Option<u64>,
}

impl VadState {
    fn new(sample_rate: f32) -> Self {
        Self {
            sample_rate,
            frame_duration_ms: FRAME_SIZE as f32 / sample_rate * 1000.0,
            pending: Vec::with_capacity(FRAME_SIZE * 2),
            noise_floor: MIN_THRESHOLD,
            calibration_count: 0,
            calibration_sum: 0.0,
            speech_active: false,
            speech_frames: Vec::new(),
            pre_roll: Vec::new(),
            silence_streak_ms: 0.0,
            speech_duration_ms: 0.0,
            audio_detected_at: None,
        }
    }

    fn process(&mut self, chunk: &[f32], app: &AppHandle, state: &SharedState, base_url: &str) {
        self.pending.extend_from_slice(chunk);
        while self.pending.len() >= FRAME_SIZE {
            let frame: Vec<f32> = self.pending.drain(..FRAME_SIZE).collect();
            self.process_frame(frame, app, state, base_url);
        }
    }

    fn process_frame(
        &mut self,
        frame: Vec<f32>,
        app: &AppHandle,
        state: &SharedState,
        base_url: &str,
    ) {
        let level = rms(&frame);
        let _ = app.emit("tars://wake-audio-level", (level * 8.0).min(1.0));

        if self.calibration_count < CALIBRATION_FRAMES {
            self.calibration_count += 1;
            self.calibration_sum += level;
            self.noise_floor =
                ((self.calibration_sum / self.calibration_count as f32) * NOISE_FLOOR_MULTIPLIER)
                    .max(MIN_THRESHOLD);
            return;
        }

        let threshold = self.noise_floor.max(MIN_THRESHOLD);
        let is_speech_frame = level >= threshold;

        if !self.speech_active {
            self.pre_roll.push(frame.clone());
            if self.pre_roll.len() > PRE_ROLL_FRAMES {
                self.pre_roll.remove(0);
            }
            if is_speech_frame {
                self.speech_active = true;
                let detected_at = epoch_ms();
                self.audio_detected_at = Some(detected_at);
                *state.state.lock().unwrap() = WakeState::Audio;
                let _ = app.emit("tars://speech-start", ());
                let _ = app.emit(
                    "tars://wake-state-changed",
                    WakeTimingTelemetry {
                        state: WakeState::Audio,
                        audio_detected_at: Some(detected_at),
                        speech_end_at: None,
                        transcription_start: None,
                        transcription_complete: None,
                        wake_detected_at: None,
                        command_ready_at: None,
                        duration_ms: None,
                        transcript: None,
                        telemetry_id: None,
                    },
                );
                self.speech_frames = self.pre_roll.concat();
                self.speech_frames.extend_from_slice(&frame);
                self.speech_duration_ms =
                    (self.speech_frames.len() as f32 / self.sample_rate) * 1000.0;
                self.silence_streak_ms = 0.0;
            }
            return;
        }

        self.speech_frames.extend_from_slice(&frame);
        self.speech_duration_ms += self.frame_duration_ms;

        if is_speech_frame {
            self.silence_streak_ms = 0.0;
        } else {
            self.silence_streak_ms += self.frame_duration_ms;
        }

        let trailing_silence_long_enough = self.silence_streak_ms >= SILENCE_HANG_MS;
        let utterance_too_long = self.speech_duration_ms >= MAX_UTTERANCE_MS;

        if trailing_silence_long_enough || utterance_too_long {
            self.finalize_utterance(app, state, base_url);
        }
    }

    fn finalize_utterance(&mut self, app: &AppHandle, state: &SharedState, base_url: &str) {
        let duration_ms = self.speech_duration_ms;
        let samples = std::mem::take(&mut self.speech_frames);
        let audio_detected_at = self.audio_detected_at.take();
        let speech_end_at = epoch_ms();
        self.speech_active = false;
        self.pre_roll.clear();
        self.silence_streak_ms = 0.0;
        self.speech_duration_ms = 0.0;

        if duration_ms < MIN_UTTERANCE_MS || samples.is_empty() {
            *state.state.lock().unwrap() = WakeState::Idle;
            let _ = app.emit(
                "tars://wake-state-changed",
                WakeTimingTelemetry {
                    state: WakeState::Idle,
                    audio_detected_at,
                    speech_end_at: Some(speech_end_at),
                    transcription_start: None,
                    transcription_complete: None,
                    wake_detected_at: None,
                    command_ready_at: None,
                    duration_ms: Some(duration_ms),
                    transcript: None,
                    telemetry_id: None,
                },
            );
            return;
        }

        let app = app.clone();
        let state_arc = shared();
        let sample_rate = self.sample_rate as u32;
        let base_url = base_url.to_string();

        *state_arc.state.lock().unwrap() = WakeState::Transcribing;
        let transcription_start = epoch_ms();
        let _ = app.emit(
            "tars://wake-state-changed",
            WakeTimingTelemetry {
                state: WakeState::Transcribing,
                audio_detected_at,
                speech_end_at: Some(speech_end_at),
                transcription_start: Some(transcription_start),
                transcription_complete: None,
                wake_detected_at: None,
                command_ready_at: None,
                duration_ms: Some(duration_ms),
                transcript: None,
                telemetry_id: None,
            },
        );

        std::thread::spawn(move || {
            handle_utterance(
                app,
                state_arc,
                samples,
                sample_rate,
                base_url,
                audio_detected_at,
                speech_end_at,
                transcription_start,
                duration_ms,
            );
        });
    }
}

fn rms(samples: &[f32]) -> f32 {
    if samples.is_empty() {
        return 0.0;
    }
    let sum_sq: f32 = samples.iter().map(|s| s * s).sum();
    (sum_sq / samples.len() as f32).sqrt()
}

fn handle_utterance(
    app: AppHandle,
    state: Arc<SharedState>,
    samples: Vec<f32>,
    sample_rate: u32,
    base_url: String,
    audio_detected_at: Option<u64>,
    speech_end_at: u64,
    transcription_start: u64,
    duration_ms: f32,
) {
    let pcm: Vec<i16> = samples
        .iter()
        .map(|s| (s.clamp(-1.0, 1.0) * 32767.0) as i16)
        .collect();
    let wav = encode_wav_i16(&pcm, sample_rate);

    let (transcript, telemetry_id) = match transcribe(&base_url, &wav) {
        Ok((text, trace_id)) => (text.trim().to_string(), trace_id),
        Err(err) => {
            eprintln!("[wake_engine] transcription failed: {err}");
            *state.state.lock().unwrap() = WakeState::Idle;
            let _ = app.emit(
                "tars://wake-state-changed",
                WakeTimingTelemetry {
                    state: WakeState::Idle,
                    audio_detected_at,
                    speech_end_at: Some(speech_end_at),
                    transcription_start: Some(transcription_start),
                    transcription_complete: None,
                    wake_detected_at: None,
                    command_ready_at: None,
                    duration_ms: Some(duration_ms),
                    transcript: None,
                    telemetry_id: None,
                },
            );
            return;
        }
    };

    let transcription_complete = epoch_ms();

    if transcript.is_empty() {
        *state.state.lock().unwrap() = WakeState::Idle;
        let _ = app.emit(
            "tars://wake-state-changed",
            WakeTimingTelemetry {
                state: WakeState::Idle,
                audio_detected_at,
                speech_end_at: Some(speech_end_at),
                transcription_start: Some(transcription_start),
                transcription_complete: Some(transcription_complete),
                wake_detected_at: None,
                command_ready_at: None,
                duration_ms: Some(duration_ms),
                transcript: None,
                telemetry_id: telemetry_id.clone(),
            },
        );
        return;
    }

    let is_command_listening = {
        let deadline = state.command_deadline.lock().unwrap();
        deadline.is_some()
    };

    let now = Instant::now();
    let now_ms = epoch_ms();

    // 1. If already in CommandListening mode from a previous "Hey TARS" [pause]:
    if is_command_listening {
        *state.command_deadline.lock().unwrap() = None;
        *state.state.lock().unwrap() = WakeState::Processing;

        let _ = app.emit(
            "tars://wake-state-changed",
            WakeTimingTelemetry {
                state: WakeState::Processing,
                audio_detected_at,
                speech_end_at: Some(speech_end_at),
                transcription_start: Some(transcription_start),
                transcription_complete: Some(transcription_complete),
                wake_detected_at: Some(now_ms),
                command_ready_at: Some(now_ms),
                duration_ms: Some(duration_ms),
                transcript: Some(transcript.clone()),
                telemetry_id: telemetry_id.clone(),
            },
        );

        if analyze_chart_regex().is_match(&transcript) {
            let _ = app.emit(
                "tars://analyze-chart-detected",
                TextPayload { text: transcript },
            );
        } else {
            let _ = app.emit(
                "tars://command-transcript",
                TextPayload { text: transcript },
            );
        }
        return;
    }

    // 2. Wake word match (single-utterance command-transcript or two-stage):
    if wake_regex().is_match(&transcript) {
        // Handle command-transcript extraction for single-utterance requests
        let wake_detected_at = now_ms;
        let command_ready_at = epoch_ms();

        // Extract command tail following the wake phrase
        let wake_match = wake_regex().find(&transcript).unwrap();
        let tail = transcript[wake_match.end()..].trim();
        let clean_tail = tail
            .trim_start_matches(|c: char| c == ',' || c == ':' || c == '-' || c.is_whitespace())
            .trim()
            .to_string();

        let has_tail = !clean_tail.is_empty();
        let is_chart = analyze_chart_regex().is_match(&clean_tail);

        if has_tail {
            // SINGLE-UTTERANCE: "Hey TARS, analyze the chart" or "Hey TARS, check market risk"
            // EXACTLY ONE CANONICAL DISPATCH EVENT IS EMITTED.
            *state.state.lock().unwrap() = WakeState::Processing;

            let _ = app.emit(
                "tars://wake-state-changed",
                WakeTimingTelemetry {
                    state: WakeState::Processing,
                    audio_detected_at,
                    speech_end_at: Some(speech_end_at),
                    transcription_start: Some(transcription_start),
                    transcription_complete: Some(transcription_complete),
                    wake_detected_at: Some(wake_detected_at),
                    command_ready_at: Some(command_ready_at),
                    duration_ms: Some(duration_ms),
                    transcript: Some(clean_tail.clone()),
                    telemetry_id: telemetry_id.clone(),
                },
            );

            if is_chart {
                let _ = app.emit(
                    "tars://analyze-chart-detected",
                    TextPayload { text: clean_tail },
                );
            } else {
                let _ = app.emit(
                    "tars://command-transcript",
                    TextPayload { text: clean_tail },
                );
            }
            return;
        }

        // TWO-STAGE: User only spoke "Hey TARS" -> await command
        *state.state.lock().unwrap() = WakeState::CommandListening;
        *state.command_deadline.lock().unwrap() =
            Some(now + Duration::from_millis(COMMAND_TIMEOUT_MS));

        let _ = app.emit(
            "tars://wake-state-changed",
            WakeTimingTelemetry {
                state: WakeState::CommandListening,
                audio_detected_at,
                speech_end_at: Some(speech_end_at),
                transcription_start: Some(transcription_start),
                transcription_complete: Some(transcription_complete),
                wake_detected_at: Some(wake_detected_at),
                command_ready_at: None,
                duration_ms: Some(duration_ms),
                transcript: Some(transcript.clone()),
                telemetry_id: telemetry_id.clone(),
            },
        );

        let _ = app.emit(
            "tars://wake-detected",
            TextPayload { text: transcript },
        );
        return;
    }

    // 3. Direct chart analysis command without wake phrase (e.g. "Analyze the chart"):
    if analyze_chart_regex().is_match(&transcript) {
        let mut last = state.last_chart_at.lock().unwrap();
        let cooldown_ok = last
            .map(|t| now.duration_since(t).as_millis() as u64 > WAKE_COOLDOWN_MS)
            .unwrap_or(true);
        if cooldown_ok {
            *last = Some(now);
            drop(last);
            *state.state.lock().unwrap() = WakeState::Processing;

            let _ = app.emit(
                "tars://wake-state-changed",
                WakeTimingTelemetry {
                    state: WakeState::Processing,
                    audio_detected_at,
                    speech_end_at: Some(speech_end_at),
                    transcription_start: Some(transcription_start),
                    transcription_complete: Some(transcription_complete),
                    wake_detected_at: Some(now_ms),
                    command_ready_at: Some(now_ms),
                    duration_ms: Some(duration_ms),
                    transcript: Some(transcript.clone()),
                    telemetry_id: telemetry_id.clone(),
                },
            );

            let _ = app.emit(
                "tars://analyze-chart-detected",
                TextPayload { text: transcript },
            );
        } else {
            *state.state.lock().unwrap() = WakeState::Idle;
            let _ = app.emit(
                "tars://wake-state-changed",
                WakeTimingTelemetry {
                    state: WakeState::Idle,
                    audio_detected_at,
                    speech_end_at: Some(speech_end_at),
                    transcription_start: Some(transcription_start),
                    transcription_complete: Some(transcription_complete),
                    wake_detected_at: None,
                    command_ready_at: None,
                    duration_ms: Some(duration_ms),
                    transcript: Some(transcript),
                    telemetry_id: telemetry_id.clone(),
                },
            );
        }
        return;
    }

    // No wake phrase detected
    *state.state.lock().unwrap() = WakeState::Idle;
    let _ = app.emit(
        "tars://wake-state-changed",
        WakeTimingTelemetry {
            state: WakeState::Idle,
            audio_detected_at,
            speech_end_at: Some(speech_end_at),
            transcription_start: Some(transcription_start),
            transcription_complete: Some(transcription_complete),
            wake_detected_at: None,
            command_ready_at: None,
            duration_ms: Some(duration_ms),
            transcript: Some(transcript),
            telemetry_id,
        },
    );
}

fn encode_wav_i16(samples: &[i16], sample_rate: u32) -> Vec<u8> {
    let num_channels: u16 = 1;
    let bits_per_sample: u16 = 16;
    let byte_rate = sample_rate * num_channels as u32 * (bits_per_sample as u32 / 8);
    let block_align = num_channels * (bits_per_sample / 8);
    let data_size = (samples.len() * 2) as u32;
    let mut buf = Vec::with_capacity(44 + data_size as usize);
    buf.extend_from_slice(b"RIFF");
    buf.extend_from_slice(&(36 + data_size).to_le_bytes());
    buf.extend_from_slice(b"WAVE");
    buf.extend_from_slice(b"fmt ");
    buf.extend_from_slice(&16u32.to_le_bytes());
    buf.extend_from_slice(&1u16.to_le_bytes());
    buf.extend_from_slice(&num_channels.to_le_bytes());
    buf.extend_from_slice(&sample_rate.to_le_bytes());
    buf.extend_from_slice(&byte_rate.to_le_bytes());
    buf.extend_from_slice(&block_align.to_le_bytes());
    buf.extend_from_slice(&bits_per_sample.to_le_bytes());
    buf.extend_from_slice(b"data");
    buf.extend_from_slice(&data_size.to_le_bytes());
    for s in samples {
        buf.extend_from_slice(&s.to_le_bytes());
    }
    buf
}

fn transcribe(base_url: &str, wav_bytes: &[u8]) -> Result<(String, Option<String>), String> {
    let boundary = "----tarswakeboundary7d8f3a";
    let mut body = Vec::with_capacity(wav_bytes.len() + 256);
    body.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
    body.extend_from_slice(
        b"Content-Disposition: form-data; name=\"file\"; filename=\"utterance.wav\"\r\n",
    );
    body.extend_from_slice(b"Content-Type: audio/wav\r\n\r\n");
    body.extend_from_slice(wav_bytes);
    body.extend_from_slice(format!("\r\n--{boundary}--\r\n").as_bytes());

    let url = format!("{base_url}/api/v1/voice/transcribe");
    let response = ureq::post(&url)
        .set(
            "Content-Type",
            &format!("multipart/form-data; boundary={boundary}"),
        )
        .timeout(Duration::from_secs(10))
        .send_bytes(&body)
        .map_err(|e| e.to_string())?;

    let text_body = response.into_string().map_err(|e| e.to_string())?;
    let json: serde_json::Value =
        serde_json::from_str(&text_body).map_err(|e| e.to_string())?;
    let text = json
        .get("text")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let telemetry_id = json
        .get("telemetry_id")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());
    Ok((text, telemetry_id))
}
