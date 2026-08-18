//! Native background wake-word runtime.
//!
//! Owns the microphone directly via `cpal` on its own OS thread so "Hey
//! TARS" detection is independent of the React panel's lifecycle: hiding,
//! closing, or never having opened the panel does not stop this engine --
//! only process exit (tray Quit) does. The panel only ever renders state
//! pushed to it through Tauri events; it never touches audio itself.
//!
//! Ports the same energy-based VAD + local-whisper-transcribe + regex wake
//! match design that previously lived in the frontend
//! (services/local-vad.ts + services/wake-word.ts) into Rust, using
//! identical thresholds/timings, so detection behavior doesn't silently
//! change -- only its independence from the webview does. Never uses cloud
//! speech recognition: audio only ever leaves this process as a WAV upload
//! to this machine's own backend at 127.0.0.1.
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use regex::Regex;
use serde::Serialize;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, RecvTimeoutError};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};
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

#[derive(Clone, Copy, PartialEq, Debug)]
enum Mode {
    Wake,
    Command,
}

struct SharedState {
    mode: Mutex<Mode>,
    command_deadline: Mutex<Option<Instant>>,
    last_wake_at: Mutex<Option<Instant>>,
    last_chart_at: Mutex<Option<Instant>>,
}

impl SharedState {
    fn new() -> Self {
        Self {
            mode: Mutex::new(Mode::Wake),
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

/// Forces the engine into one-shot command-capture mode immediately --
/// used for barge-in: the moment the frontend hears `speech-start` while
/// TARS is speaking, it calls this so the utterance already forming is
/// captured as the user's next command instead of being wake-word-filtered.
pub fn force_command_capture() {
    let state = shared();
    *state.mode.lock().unwrap() = Mode::Command;
    *state.command_deadline.lock().unwrap() =
        Some(Instant::now() + Duration::from_millis(COMMAND_TIMEOUT_MS));
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
            *state.mode.lock().unwrap() = Mode::Wake;
            let _ = app.emit("tars://command-timeout", ());
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
                let _ = app.emit("tars://speech-start", ());
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
        self.speech_active = false;
        self.pre_roll.clear();
        self.silence_streak_ms = 0.0;
        self.speech_duration_ms = 0.0;

        if duration_ms < MIN_UTTERANCE_MS || samples.is_empty() {
            return;
        }

        let app = app.clone();
        let mode_at_capture = *state.mode.lock().unwrap();
        let sample_rate = self.sample_rate as u32;
        let base_url = base_url.to_string();
        let state_arc = shared();

        // Transcription (network round trip) happens off this thread so the
        // continuous VAD loop keeps consuming audio without gaps while it
        // waits on the backend.
        std::thread::spawn(move || {
            handle_utterance(app, state_arc, mode_at_capture, samples, sample_rate, base_url);
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
    mode_at_capture: Mode,
    samples: Vec<f32>,
    sample_rate: u32,
    base_url: String,
) {
    let pcm: Vec<i16> = samples
        .iter()
        .map(|s| (s.clamp(-1.0, 1.0) * 32767.0) as i16)
        .collect();
    let wav = encode_wav_i16(&pcm, sample_rate);

    let transcript = match transcribe(&base_url, &wav) {
        Ok(text) => text.trim().to_string(),
        Err(err) => {
            eprintln!("[wake_engine] transcription failed: {err}");
            return;
        }
    };

    if transcript.is_empty() {
        return;
    }

    // Mode may have changed since this utterance's onset (e.g.
    // force_command_capture fired mid-utterance for barge-in) -- the
    // current mode always wins over the one captured at onset.
    let current_mode = *state.mode.lock().unwrap();
    let effective_mode = if current_mode == Mode::Command {
        Mode::Command
    } else {
        mode_at_capture
    };

    if effective_mode == Mode::Command {
        *state.mode.lock().unwrap() = Mode::Wake;
        *state.command_deadline.lock().unwrap() = None;
        let _ = app.emit(
            "tars://command-transcript",
            TextPayload { text: transcript },
        );
        return;
    }

    let now = Instant::now();

    if analyze_chart_regex().is_match(&transcript) {
        let mut last = state.last_chart_at.lock().unwrap();
        let cooldown_ok = last
            .map(|t| now.duration_since(t).as_millis() as u64 > WAKE_COOLDOWN_MS)
            .unwrap_or(true);
        if cooldown_ok {
            *last = Some(now);
            drop(last);
            let _ = app.emit(
                "tars://analyze-chart-detected",
                TextPayload { text: transcript },
            );
        }
        return;
    }

    if wake_regex().is_match(&transcript) {
        let mut last = state.last_wake_at.lock().unwrap();
        let cooldown_ok = last
            .map(|t| now.duration_since(t).as_millis() as u64 > WAKE_COOLDOWN_MS)
            .unwrap_or(true);
        if cooldown_ok {
            *last = Some(now);
            drop(last);
            *state.mode.lock().unwrap() = Mode::Command;
            *state.command_deadline.lock().unwrap() =
                Some(now + Duration::from_millis(COMMAND_TIMEOUT_MS));
            let _ = app.emit("tars://wake-detected", TextPayload { text: transcript });
        }
    }
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

fn transcribe(base_url: &str, wav_bytes: &[u8]) -> Result<String, String> {
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
    Ok(json
        .get("text")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string())
}
