//! Continuous native capture transport. The backend owns VAD, endpointing,
//! recognition, generation IDs and conversational state. No capture muting
//! while speaking: microphone frames continue through every assistant turn.
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{sync_channel, RecvTimeoutError};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use serde::Serialize;
use tauri::{AppHandle, Emitter};

static RUNNING: AtomicBool = AtomicBool::new(false);
static LAST_ERROR: Mutex<Option<String>> = Mutex::new(None);
/// Preferred input device name (saved TARS preference); empty = automatic.
static PREFERRED: Mutex<String> = Mutex::new(String::new());
static RESTART: AtomicBool = AtomicBool::new(false);
/// Authoritative microphone mute. While set, NO audio frame leaves this thread: nothing reaches the
/// webview, the backend VAD, Gemini or STT. Level metering for diagnostics continues locally.
static MUTED: AtomicBool = AtomicBool::new(false);

pub fn is_muted() -> bool { MUTED.load(Ordering::SeqCst) }
pub fn set_muted_flag(muted: bool) { MUTED.store(muted, Ordering::SeqCst); }
static INFO: Mutex<Option<MicInfo>> = Mutex::new(None);

/// Digital silence threshold: even a quiet room sits far above this on a working microphone path.
const SILENT_DB: f64 = -85.0;

#[derive(Debug, Clone, Serialize)]
pub struct MicInfo {
    /// OPEN | STARTING | NO_DEVICE | ERROR
    pub status: String,
    pub device: String,
    pub selected_by: String,
    pub sample_rate: u32,
    pub channels: u16,
    pub format: String,
    pub frames_total: u64,
    pub frames_per_sec: f64,
    /// Smoothed RMS of the last frames, dBFS (-120 = digital silence).
    pub rms_db: f64,
    /// Loudest frame RMS since the device was opened.
    pub max_db: f64,
    /// Seconds since the level was last above the digital-silence threshold.
    pub silent_for_s: f64,
    pub open_for_s: f64,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct InputDevice {
    pub name: String,
    pub is_default: bool,
}

pub fn list_devices() -> Vec<InputDevice> {
    let host = cpal::default_host();
    let default_name = host.default_input_device().and_then(|d| dev_name(&d)).unwrap_or_default();
    host.input_devices()
        .map(|it| {
            it.filter_map(|d| dev_name(&d))
                .map(|name| InputDevice { is_default: name == default_name, name })
                .collect()
        })
        .unwrap_or_default()
}

pub fn info() -> Option<MicInfo> { INFO.lock().unwrap().clone() }

/// Save the preferred microphone and reopen capture on it ("" = automatic).
pub fn set_preferred(name: String) {
    *PREFERRED.lock().unwrap() = name;
    RESTART.store(true, Ordering::SeqCst);
}

fn dev_name(d: &cpal::Device) -> Option<String> { d.description().ok().map(|x| x.name().to_string()) }

fn db(rms: f64) -> f64 { if rms <= 1e-6 { -120.0 } else { (20.0 * rms.log10()).max(-120.0) } }

/// Order: saved TARS choice, Windows default input, then the first valid device.
/// (cpal does not expose the separate "default communications" endpoint; the default input is used.)
fn pick_device() -> Option<(cpal::Device, String)> {
    let host = cpal::default_host();
    let preferred = PREFERRED.lock().unwrap().clone();
    if !preferred.is_empty() {
        if let Ok(devs) = host.input_devices() {
            for d in devs {
                if dev_name(&d).map(|n| n == preferred).unwrap_or(false) && d.default_input_config().is_ok() {
                    return Some((d, "saved preference".into()));
                }
            }
        }
        eprintln!("[wake_engine] preferred microphone '{preferred}' not found; falling back");
    }
    if let Some(d) = host.default_input_device() {
        if d.default_input_config().is_ok() { return Some((d, "Windows default input".into())); }
    }
    host.input_devices().ok()?.find(|d| d.default_input_config().is_ok()).map(|d| (d, "first valid device (fallback)".into()))
}

pub fn is_running() -> bool { RUNNING.load(Ordering::SeqCst) }
pub fn last_error() -> Option<String> { LAST_ERROR.lock().unwrap().clone() }
// Retained command compatibility only; playback never gates microphone capture.
pub fn set_playback_speaking(_speaking: bool, _app: &AppHandle) {}

pub fn start(app: AppHandle) {
    if RUNNING.swap(true, Ordering::SeqCst) { return; }
    std::thread::spawn(move || loop {
        match run(&app) {
            Ok(()) => continue, // deliberate restart (device preference changed)
            Err(err) => {
                eprintln!("[wake_engine] capture disconnected: {err}");
                *LAST_ERROR.lock().unwrap() = Some(err.clone());
                RUNNING.store(false, Ordering::SeqCst);
                if let Ok(mut g) = INFO.lock() {
                    let mut i = g.clone().unwrap_or(MicInfo {
                        status: "ERROR".into(), device: String::new(), selected_by: String::new(), sample_rate: 0, channels: 0,
                        format: String::new(), frames_total: 0, frames_per_sec: 0.0, rms_db: -120.0, max_db: -120.0,
                        silent_for_s: 0.0, open_for_s: 0.0, error: None,
                    });
                    i.status = if err.contains("no microphone") { "NO_DEVICE".into() } else { "ERROR".into() };
                    i.error = Some(err.clone());
                    i.frames_per_sec = 0.0;
                    let _ = app.emit("tars://microphone-info", &i);
                    *g = Some(i);
                }
                let _ = app.emit("tars://microphone-status", "DISCONNECTED");
            }
        }
        // Device unplug/replug is recoverable without restarting TARS.
        std::thread::sleep(Duration::from_secs(3));
        RUNNING.store(true, Ordering::SeqCst);
    });
}

fn run(app: &AppHandle) -> Result<(), String> {
    RESTART.store(false, Ordering::SeqCst);
    let (device, selected_by) = match pick_device() {
        Some(pair) => pair,
        None => {
            *INFO.lock().unwrap() = Some(MicInfo {
                status: "NO_DEVICE".into(), device: String::new(), selected_by: String::new(), sample_rate: 0, channels: 0,
                format: String::new(), frames_total: 0, frames_per_sec: 0.0, rms_db: -120.0, max_db: -120.0,
                silent_for_s: 0.0, open_for_s: 0.0, error: Some("no microphone found".into()),
            });
            let _ = app.emit("tars://microphone-info", info());
            return Err("no microphone found".into());
        }
    };
    let device_name = dev_name(&device).unwrap_or_else(|| "unknown device".into());
    let supported = device.default_input_config().map_err(|e| e.to_string())?;
    let format = supported.sample_format();
    let config: cpal::StreamConfig = supported.into();
    let channels = config.channels as usize;
    let sample_rate = config.sample_rate;
    eprintln!("[wake_engine] mic opening device='{device_name}' via {selected_by} rate={sample_rate} channels={channels} format={format:?}");
    let (tx, rx) = sync_channel::<Vec<f32>>(16);
    let (errors, error_rx) = sync_channel::<String>(8);
    let stream = match format {
        cpal::SampleFormat::F32 => device.build_input_stream(
            config.clone(), move |data: &[f32], _: &cpal::InputCallbackInfo| {
                let _ = tx.try_send(downmix(data, channels));
            }, move |err| { let _ = errors.try_send(err.to_string()); }, None),
        cpal::SampleFormat::I16 => device.build_input_stream(
            config.clone(), move |data: &[i16], _: &cpal::InputCallbackInfo| {
                let floats: Vec<f32> = data.iter().map(|v| *v as f32 / 32768.0).collect();
                let _ = tx.try_send(downmix(&floats, channels));
            }, move |err| { let _ = errors.try_send(err.to_string()); }, None),
        other => return Err(format!("unsupported microphone format: {other:?}")),
    }.map_err(|e| e.to_string())?;
    stream.play().map_err(|e| e.to_string())?;
    *LAST_ERROR.lock().unwrap() = None;
    let _ = app.emit("tars://microphone-status", "CONNECTED");
    let mut resampler = Resampler::new(sample_rate);
    let mut pending: Vec<i16> = Vec::new();
    let opened = Instant::now();
    let mut last_signal = Instant::now();
    let mut last_frame = Instant::now();
    let (mut frames_total, mut window_frames) = (0u64, 0u64);
    let (mut smooth_db, mut max_db) = (-120.0f64, -120.0f64);
    let (mut last_emit, mut last_log, mut window_start) = (Instant::now(), Instant::now(), Instant::now());
    let mut fps = 0.0f64;
    let mut xruns = 0u64;
    let fmt = format!("{format:?}");
    loop {
        if RESTART.load(Ordering::SeqCst) {
            eprintln!("[wake_engine] restarting capture (device preference changed)");
            return Ok(());
        }
        if let Ok(err) = error_rx.try_recv() {
            // A buffer under/overrun is a glitch, not a dead device: count it and keep capturing.
            if err.contains("underrun or overrun") {
                xruns += 1;
                if xruns <= 3 || xruns % 100 == 0 { eprintln!("[wake_engine] capture xrun #{xruns}: {err}"); }
            } else {
                return Err(err);
            }
        }
        match rx.recv_timeout(Duration::from_millis(500)) {
            Ok(chunk) => {
                pending.extend(resampler.push(&chunk));
                while pending.len() >= 512 {
                    let frame: Vec<i16> = pending.drain(..512).collect();
                    let energy = (frame.iter().map(|v| (*v as f32 / 32768.0).powi(2)).sum::<f32>() / 512.0).sqrt();
                    let frame_db = db(energy as f64);
                    frames_total += 1;
                    window_frames += 1;
                    last_frame = Instant::now();
                    smooth_db = if frame_db > smooth_db { frame_db } else { smooth_db * 0.9 + frame_db * 0.1 };
                    if frame_db > max_db { max_db = frame_db; }
                    if frame_db > SILENT_DB { last_signal = Instant::now(); }
                    if MUTED.load(Ordering::SeqCst) {
                        let _ = app.emit("tars://wake-audio-level", 0.0f32);
                    } else {
                        let _ = app.emit("tars://wake-audio-level", (energy * 8.0).min(1.0));
                        let _ = app.emit("tars://microphone-pcm", frame);
                    }
                }
            }
            Err(RecvTimeoutError::Timeout) => {
                // Opening the device is not proof of capture, but drivers can take a few seconds to
                // deliver the first frame: wait up to 6 s for it, then 2 s of silence between frames.
                let since_frame = last_frame.elapsed();
                let limit = if frames_total == 0 { Duration::from_secs(6) } else { Duration::from_secs(2) };
                if since_frame >= limit {
                    return Err(if frames_total == 0 { "microphone opened but delivered no audio frames".into() } else { "microphone stopped delivering audio".into() });
                }
            }
            Err(RecvTimeoutError::Disconnected) => return Err("microphone channel disconnected".into()),
        }
        if window_start.elapsed() >= Duration::from_secs(1) {
            fps = window_frames as f64 / window_start.elapsed().as_secs_f64();
            window_frames = 0;
            window_start = Instant::now();
        }
        if last_emit.elapsed() >= Duration::from_millis(400) {
            last_emit = Instant::now();
            let snapshot = MicInfo {
                status: if frames_total > 0 { "OPEN".into() } else { "STARTING".into() }, device: device_name.clone(), selected_by: selected_by.clone(), sample_rate,
                channels: channels as u16, format: fmt.clone(), frames_total, frames_per_sec: fps, rms_db: smooth_db,
                max_db, silent_for_s: last_signal.elapsed().as_secs_f64(), open_for_s: opened.elapsed().as_secs_f64(), error: None,
            };
            let _ = app.emit("tars://microphone-info", &snapshot);
            *INFO.lock().unwrap() = Some(snapshot);
        }
        if last_log.elapsed() >= Duration::from_secs(10) {
            last_log = Instant::now();
            // Levels and counters only: never audio content.
            eprintln!("[wake_engine] mic device='{device_name}' frames={frames_total} fps={fps:.1} rms={smooth_db:.1}dBFS max={max_db:.1}dBFS silent_for={:.0}s", last_signal.elapsed().as_secs_f64());
        }
    }
}

fn downmix(data: &[f32], channels: usize) -> Vec<f32> {
    data.chunks(channels.max(1))
        .map(|frame| frame.iter().sum::<f32>() / frame.len() as f32).collect()
}

// Stateful linear interpolation preserves fractional position across device
// callbacks (44.1kHz as well as 48kHz); output is exactly PCM16 mono 16kHz.
struct Resampler { samples: Vec<f32>, position: f64, step: f64 }
impl Resampler {
    fn new(rate: u32) -> Self {
        Self { samples: Vec::new(), position: 0.0, step: rate as f64 / 16000.0 }
    }
    fn push(&mut self, chunk: &[f32]) -> Vec<i16> {
        self.samples.extend_from_slice(chunk);
        let mut result = Vec::new();
        while self.position + 1.0 < self.samples.len() as f64 {
            let index = self.position.floor() as usize;
            let fraction = (self.position - index as f64) as f32;
            let value = self.samples[index] * (1.0 - fraction) + self.samples[index + 1] * fraction;
            result.push((value.clamp(-1.0, 1.0) * 32767.0) as i16);
            self.position += self.step;
        }
        let consumed = (self.position.floor() as usize).min(self.samples.len());
        self.samples.drain(..consumed);
        self.position -= consumed as f64;
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn resampling_preserves_rate_across_callbacks() {
        for rate in [44100, 48000, 16000] {
            let mut resampler = Resampler::new(rate);
            let mut output = Vec::new();
            for _ in 0..100 { output.extend(resampler.push(&vec![0.25; rate as usize / 100])); }
            assert!((output.len() as i32 - 16000).abs() <= 1);
            assert!(output.iter().all(|v| (*v as i32 - 8191).abs() <= 1));
        }
    }
    #[test]
    fn stereo_downmix_is_mono() { assert_eq!(downmix(&[1.0, -1.0, 0.5, 0.5], 2), vec![0.0, 0.5]); }
}
