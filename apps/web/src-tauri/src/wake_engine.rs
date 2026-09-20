//! Continuous native capture transport. The backend owns VAD, endpointing,
//! recognition, generation IDs and conversational state. No capture muting
//! while speaking: microphone frames continue through every assistant turn.
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{sync_channel, RecvTimeoutError};
use std::sync::Mutex;
use std::time::Duration;
use tauri::{AppHandle, Emitter};

static RUNNING: AtomicBool = AtomicBool::new(false);
static LAST_ERROR: Mutex<Option<String>> = Mutex::new(None);

pub fn is_running() -> bool { RUNNING.load(Ordering::SeqCst) }
pub fn last_error() -> Option<String> { LAST_ERROR.lock().unwrap().clone() }
// Retained command compatibility only; playback never gates microphone capture.
pub fn set_playback_speaking(_speaking: bool, _app: &AppHandle) {}

pub fn start(app: AppHandle) {
    if RUNNING.swap(true, Ordering::SeqCst) { return; }
    std::thread::spawn(move || loop {
        if let Err(err) = run(&app) {
            eprintln!("[wake_engine] capture disconnected: {err}");
            *LAST_ERROR.lock().unwrap() = Some(err.clone());
            RUNNING.store(false, Ordering::SeqCst);
            let _ = app.emit("tars://microphone-status", "DISCONNECTED");
        }
        // Device unplug/replug is recoverable without restarting TARS.
        std::thread::sleep(Duration::from_secs(3));
        RUNNING.store(true, Ordering::SeqCst);
    });
}

fn run(app: &AppHandle) -> Result<(), String> {
    let device = cpal::default_host().default_input_device()
        .ok_or("no default microphone")?;
    let supported = device.default_input_config().map_err(|e| e.to_string())?;
    let format = supported.sample_format();
    let config: cpal::StreamConfig = supported.into();
    let channels = config.channels as usize;
    let sample_rate = config.sample_rate;
    let (tx, rx) = sync_channel::<Vec<f32>>(16);
    let (errors, error_rx) = sync_channel::<String>(1);
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
    loop {
        if let Ok(err) = error_rx.try_recv() { return Err(err); }
        match rx.recv_timeout(Duration::from_secs(2)) {
            Ok(chunk) => {
                pending.extend(resampler.push(&chunk));
                while pending.len() >= 512 {
                    let frame: Vec<i16> = pending.drain(..512).collect();
                    let energy = (frame.iter().map(|v| (*v as f32 / 32768.0).powi(2))
                        .sum::<f32>() / 512.0).sqrt();
                    let _ = app.emit("tars://wake-audio-level", (energy * 8.0).min(1.0));
                    let _ = app.emit("tars://microphone-pcm", frame);
                }
            },
            Err(RecvTimeoutError::Timeout) => return Err("microphone stopped delivering audio".into()),
            Err(RecvTimeoutError::Disconnected) => return Err("microphone channel disconnected".into()),
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
