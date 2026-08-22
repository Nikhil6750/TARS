//! Native microphone transport for the minimal golden voice loop.
//!
//! This module owns only device capture, energy-based segmentation, WAV
//! encoding, upload, and playback-state transport. Wake matching, command
//! capture, routing, provider selection, response composition, and TTS all
//! belong to the backend `AssistantTurnController` reached through the one
//! canonical `/api/v1/voice/utterance` endpoint.

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use serde::{Deserialize, Serialize};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{channel, RecvTimeoutError};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Emitter};

const FRAME_SIZE: usize = 2048;
const CALIBRATION_FRAMES: u32 = 12;
const NOISE_FLOOR_MULTIPLIER: f32 = 2.5;
const MIN_THRESHOLD: f32 = 0.012;
const SILENCE_HANG_MS: f32 = 700.0;
const MIN_UTTERANCE_MS: f32 = 250.0;
const MAX_UTTERANCE_MS: f32 = 9000.0;
const PRE_ROLL_FRAMES: usize = 4;

#[derive(Clone, Copy, PartialEq, Eq, Debug, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum WakeState {
    Idle,
    SpeechDetected,
    Transcribing,
    WakeDetected,
    ListeningForCommand,
    Processing,
    Speaking,
}

#[derive(Clone, Debug, Serialize)]
pub struct VoiceStateEvent {
    pub state: WakeState,
    pub turn_id: Option<String>,
    pub audio_detected_at: Option<u64>,
    pub speech_end_at: Option<u64>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AssistantResponse {
    pub turn_id: String,
    pub display_text: String,
    pub speech_text: String,
    pub intent: String,
    pub status: String,
    pub provider: String,
    pub latency_ms: f64,
    pub conversation_id: String,
    pub transcript: Option<String>,
    #[serde(default)]
    pub replayed: bool,
    #[serde(default)]
    pub audio_chunks_base64: Vec<String>,
}

struct SharedState {
    state: Mutex<WakeState>,
    after_playback: Mutex<WakeState>,
}

impl SharedState {
    fn new() -> Self {
        Self {
            state: Mutex::new(WakeState::Idle),
            after_playback: Mutex::new(WakeState::Idle),
        }
    }
}

static RUNNING: AtomicBool = AtomicBool::new(false);
static LAST_ERROR: Mutex<Option<String>> = Mutex::new(None);
static TURN_COUNTER: AtomicU64 = AtomicU64::new(0);
static SHARED: OnceLock<Arc<SharedState>> = OnceLock::new();

fn shared() -> Arc<SharedState> {
    SHARED.get_or_init(|| Arc::new(SharedState::new())).clone()
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

fn session_id() -> String {
    format!("native-{}", std::process::id())
}

fn next_turn_id() -> String {
    let count = TURN_COUNTER.fetch_add(1, Ordering::Relaxed);
    format!("native-{}-{count}", epoch_ms())
}

fn emit_state(
    app: &AppHandle,
    state: &SharedState,
    next: WakeState,
    turn_id: Option<String>,
    audio_detected_at: Option<u64>,
    speech_end_at: Option<u64>,
) {
    *state.state.lock().unwrap() = next;
    let _ = app.emit(
        "tars://wake-state-changed",
        VoiceStateEvent {
            state: next,
            turn_id,
            audio_detected_at,
            speech_end_at,
        },
    );
}

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

/// Playback is transport state only. The next assistant state was already
/// selected by the backend response (IDLE or LISTENING_FOR_COMMAND).
pub fn set_playback_speaking(speaking: bool, app: &AppHandle) {
    let state = shared();
    let next = if speaking {
        WakeState::Speaking
    } else {
        *state.after_playback.lock().unwrap()
    };
    emit_state(app, &state, next, None, None, None);
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
            Err(RecvTimeoutError::Timeout) => {}
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
        return data.iter().map(|sample| *sample as f32 / 32768.0).collect();
    }
    data.chunks(channels)
        .map(|frame| {
            frame
                .iter()
                .map(|sample| *sample as f32 / 32768.0)
                .sum::<f32>()
                / channels as f32
        })
        .collect()
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
        let runtime_state = *state.state.lock().unwrap();
        if matches!(
            runtime_state,
            WakeState::Transcribing | WakeState::Processing | WakeState::Speaking
        ) {
            self.reset_capture();
            return;
        }

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

        let is_speech_frame = level >= self.noise_floor.max(MIN_THRESHOLD);
        if !self.speech_active {
            self.pre_roll.push(frame.clone());
            if self.pre_roll.len() > PRE_ROLL_FRAMES {
                self.pre_roll.remove(0);
            }
            if is_speech_frame {
                self.speech_active = true;
                let detected_at = epoch_ms();
                self.audio_detected_at = Some(detected_at);
                emit_state(
                    app,
                    state,
                    WakeState::SpeechDetected,
                    None,
                    Some(detected_at),
                    None,
                );
                self.speech_frames = self.pre_roll.concat();
                self.speech_frames.extend_from_slice(&frame);
                self.speech_duration_ms =
                    (self.speech_frames.len() as f32 / self.sample_rate) * 1000.0;
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
        if self.silence_streak_ms >= SILENCE_HANG_MS
            || self.speech_duration_ms >= MAX_UTTERANCE_MS
        {
            self.finalize_utterance(app, state, base_url);
        }
    }

    fn finalize_utterance(&mut self, app: &AppHandle, state: &SharedState, base_url: &str) {
        let duration_ms = self.speech_duration_ms;
        let samples = std::mem::take(&mut self.speech_frames);
        let audio_detected_at = self.audio_detected_at.take();
        let speech_end_at = epoch_ms();
        self.reset_capture();

        if duration_ms < MIN_UTTERANCE_MS || samples.is_empty() {
            emit_state(
                app,
                state,
                WakeState::Idle,
                None,
                audio_detected_at,
                Some(speech_end_at),
            );
            return;
        }

        let app = app.clone();
        let state = shared();
        let sample_rate = self.sample_rate as u32;
        let base_url = base_url.to_string();
        let turn_id = next_turn_id();
        emit_state(
            &app,
            &state,
            WakeState::Transcribing,
            Some(turn_id.clone()),
            audio_detected_at,
            Some(speech_end_at),
        );
        std::thread::spawn(move || {
            handle_utterance(
                app,
                state,
                samples,
                sample_rate,
                base_url,
                turn_id,
                audio_detected_at,
                speech_end_at,
            );
        });
    }

    fn reset_capture(&mut self) {
        self.speech_active = false;
        self.speech_frames.clear();
        self.pre_roll.clear();
        self.silence_streak_ms = 0.0;
        self.speech_duration_ms = 0.0;
        self.audio_detected_at = None;
    }
}

fn rms(samples: &[f32]) -> f32 {
    if samples.is_empty() {
        return 0.0;
    }
    let sum_sq: f32 = samples.iter().map(|sample| sample * sample).sum();
    (sum_sq / samples.len() as f32).sqrt()
}

fn handle_utterance(
    app: AppHandle,
    state: Arc<SharedState>,
    samples: Vec<f32>,
    sample_rate: u32,
    base_url: String,
    turn_id: String,
    audio_detected_at: Option<u64>,
    speech_end_at: u64,
) {
    let pcm: Vec<i16> = samples
        .iter()
        .map(|sample| (sample.clamp(-1.0, 1.0) * 32767.0) as i16)
        .collect();
    let wav = encode_wav_i16(&pcm, sample_rate);
    let response = match submit_utterance(
        &base_url,
        &wav,
        &turn_id,
        audio_detected_at,
        speech_end_at,
    ) {
        Ok(response) => response,
        Err(err) => {
            eprintln!("[wake_engine] canonical utterance failed: {err}");
            *LAST_ERROR.lock().unwrap() = Some(err);
            emit_state(
                &app,
                &state,
                WakeState::Idle,
                Some(turn_id),
                audio_detected_at,
                Some(speech_end_at),
            );
            return;
        }
    };

    let after_playback = if response.status == "awaiting_command" {
        WakeState::ListeningForCommand
    } else {
        WakeState::Idle
    };
    *state.after_playback.lock().unwrap() = after_playback;
    if response.status == "awaiting_command" {
        emit_state(
            &app,
            &state,
            WakeState::WakeDetected,
            Some(response.turn_id.clone()),
            audio_detected_at,
            Some(speech_end_at),
        );
    } else if response.status == "completed" {
        emit_state(
            &app,
            &state,
            WakeState::Processing,
            Some(response.turn_id.clone()),
            audio_detected_at,
            Some(speech_end_at),
        );
    }
    let has_audio = !response.audio_chunks_base64.is_empty();
    let _ = app.emit("tars://assistant-turn-complete", response);
    emit_state(
        &app,
        &state,
        if has_audio {
            WakeState::Speaking
        } else {
            after_playback
        },
        Some(turn_id),
        audio_detected_at,
        Some(speech_end_at),
    );
}

fn encode_wav_i16(samples: &[i16], sample_rate: u32) -> Vec<u8> {
    let num_channels: u16 = 1;
    let bits_per_sample: u16 = 16;
    let byte_rate = sample_rate * num_channels as u32 * (bits_per_sample as u32 / 8);
    let block_align = num_channels * (bits_per_sample / 8);
    let data_size = (samples.len() * 2) as u32;
    let mut buffer = Vec::with_capacity(44 + data_size as usize);
    buffer.extend_from_slice(b"RIFF");
    buffer.extend_from_slice(&(36 + data_size).to_le_bytes());
    buffer.extend_from_slice(b"WAVEfmt ");
    buffer.extend_from_slice(&16u32.to_le_bytes());
    buffer.extend_from_slice(&1u16.to_le_bytes());
    buffer.extend_from_slice(&num_channels.to_le_bytes());
    buffer.extend_from_slice(&sample_rate.to_le_bytes());
    buffer.extend_from_slice(&byte_rate.to_le_bytes());
    buffer.extend_from_slice(&block_align.to_le_bytes());
    buffer.extend_from_slice(&bits_per_sample.to_le_bytes());
    buffer.extend_from_slice(b"data");
    buffer.extend_from_slice(&data_size.to_le_bytes());
    for sample in samples {
        buffer.extend_from_slice(&sample.to_le_bytes());
    }
    buffer
}

fn append_field(body: &mut Vec<u8>, boundary: &str, name: &str, value: &str) {
    body.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
    body.extend_from_slice(
        format!("Content-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n")
            .as_bytes(),
    );
}

fn submit_utterance(
    base_url: &str,
    wav_bytes: &[u8],
    turn_id: &str,
    audio_detected_at: Option<u64>,
    speech_end_at: u64,
) -> Result<AssistantResponse, String> {
    let boundary = "----tarsgoldenloop7d8f3a";
    let mut body = Vec::with_capacity(wav_bytes.len() + 1024);
    body.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
    body.extend_from_slice(
        b"Content-Disposition: form-data; name=\"file\"; filename=\"utterance.wav\"\r\n",
    );
    body.extend_from_slice(b"Content-Type: audio/wav\r\n\r\n");
    body.extend_from_slice(wav_bytes);
    body.extend_from_slice(b"\r\n");
    let session = session_id();
    append_field(&mut body, boundary, "session_id", &session);
    append_field(&mut body, boundary, "conversation_id", &session);
    append_field(&mut body, boundary, "turn_id", turn_id);
    if let Some(detected_at) = audio_detected_at {
        append_field(
            &mut body,
            boundary,
            "audio_detected_at_ms",
            &detected_at.to_string(),
        );
    }
    append_field(
        &mut body,
        boundary,
        "speech_end_at_ms",
        &speech_end_at.to_string(),
    );
    body.extend_from_slice(format!("--{boundary}--\r\n").as_bytes());

    let response = ureq::post(&format!("{base_url}/api/v1/voice/utterance"))
        .set(
            "Content-Type",
            &format!("multipart/form-data; boundary={boundary}"),
        )
        .set("X-TARS-Turn-ID", turn_id)
        .timeout(Duration::from_secs(90))
        .send_bytes(&body)
        .map_err(|error| error.to_string())?;
    let response_body = response.into_string().map_err(|error| error.to_string())?;
    serde_json::from_str(&response_body).map_err(|error| error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wav_encoder_produces_a_pcm_wav_header() {
        let wav = encode_wav_i16(&[0, 1, -1], 16_000);
        assert_eq!(&wav[0..4], b"RIFF");
        assert_eq!(&wav[8..12], b"WAVE");
        assert_eq!(&wav[36..40], b"data");
    }

    #[test]
    fn native_state_names_match_the_backend_state_contract() {
        assert_eq!(
            serde_json::to_string(&WakeState::ListeningForCommand).unwrap(),
            "\"LISTENING_FOR_COMMAND\""
        );
        assert_eq!(
            serde_json::to_string(&WakeState::SpeechDetected).unwrap(),
            "\"SPEECH_DETECTED\""
        );
    }
}
