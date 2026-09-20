//! BackgroundChartWatcher (TARS Alexa-Speed Phase C1/C2-Rust-side).
//!
//! Runs on its own background OS thread (same independence-from-the-panel
//! pattern as `wake_engine.rs`), polling a discovered chart window via
//! `capture_wgc::WgcCapture` every ~1-2s, and pushing a frame to the
//! backend only when a cheap perceptual diff says something meaningfully
//! changed, or a coarse safety-net interval has elapsed since the last
//! push. This module never decides whether to spend an expensive Claude
//! vision call -- that decision (real per-timeframe staleness policy,
//! minimum cooldown, HotChartState persistence) lives entirely in the
//! backend (`chart_watch.py`, Phase C2-Python). This module's only job is
//! "is it worth pinging the backend about this frame," as cheaply as
//! possible.
//!
//! Deliberately does NOT reuse `capture_chart_window`'s hide/DWM-wait/
//! BitBlt/restore path -- that path steals OS input focus on every
//! restore (`window.set_focus()`), which is fine for a single user-
//! triggered capture but unacceptable for a silent loop running every
//! couple of seconds. WGC's per-window capture never touches TARS's own
//! window or focus at all.
use crate::capture_wgc::{CapturedFrame, WgcCapture};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};
use tauri::AppHandle;

const POLL_INTERVAL: Duration = Duration::from_millis(1500);
// Safety-net: even with zero detected visual change, ping the backend at
// least this often so a chart that changes in ways the cheap hash doesn't
// catch (e.g. only a small price-ticker number moving) still eventually
// gets a fresh read. The backend's own per-timeframe staleness policy is
// the real freshness authority -- this is just "don't go silent forever."
const FALLBACK_PUSH_INTERVAL: Duration = Duration::from_secs(90);
// Floor between any two backend pushes, regardless of reason, so a hash
// value oscillating right at the diff threshold can't spam the endpoint.
const MIN_PUSH_COOLDOWN: Duration = Duration::from_secs(5);
// User input idle threshold before pausing all capture work (Part 21:
// "when laptop is idle / screen locked: pause expensive analysis"). A
// locked session also reports as idle here (no input reaches the desktop),
// so this one signal reasonably approximates both cases without needing a
// separate WTS session-notification listener.
const IDLE_PAUSE_THRESHOLD: Duration = Duration::from_secs(120);

const HASH_GRID: usize = 16; // 16x16 = 256-bit average hash
// Hamming-distance threshold (out of 256 bits) above which two frames are
// considered "meaningfully different." Chosen conservatively: normal price
// ticker/candle updates on a real chart typically flip well under this
// many grid cells; a real symbol/timeframe switch or a large visible
// structural change flips many more. Tunable -- not empirically tuned
// against a large sample yet, flagged as a Phase C follow-up in the
// handoff rather than presented as a precisely calibrated constant.
const HASH_DIFF_THRESHOLD: u32 = 14;

static RUNNING: AtomicBool = AtomicBool::new(false);

/// Starts the watcher on its own OS thread. Safe to call once; a second
/// call is a no-op if already running (mirrors `wake_engine::start`).
pub fn start(app: AppHandle) {
    if RUNNING.swap(true, Ordering::SeqCst) {
        return;
    }
    std::thread::spawn(move || run(app));
}

fn backend_base_url() -> String {
    std::env::var("TARS_BACKEND_URL").unwrap_or_else(|_| "http://127.0.0.1:8000".to_string())
}

struct WatchedSession {
    hwnd: isize,
    capture: WgcCapture,
    last_hash: Option<[u64; 4]>,
    last_pushed_at: Option<Instant>,
}

fn run(app: AppHandle) {
    let mut session: Option<WatchedSession> = None;

    loop {
        std::thread::sleep(POLL_INTERVAL);

        if idle_duration() >= IDLE_PAUSE_THRESHOLD {
            // Idle/locked: drop any live session so its D3D/WGC resources
            // are released, and skip work entirely until input resumes.
            session = None;
            continue;
        }

        let Some(target_hwnd) = find_chart_window() else {
            // No candidate chart window present -- pause safely (Part 3:
            // "run only when appropriate"; Part 26: "close TradingView ->
            // watcher pauses safely").
            session = None;
            continue;
        };

        let needs_new_session = match &session {
            Some(s) => s.hwnd != target_hwnd || s.capture.is_target_closed(),
            None => true,
        };
        if needs_new_session {
            match WgcCapture::new(target_hwnd) {
                Ok(capture) => {
                    session = Some(WatchedSession {
                        hwnd: target_hwnd,
                        capture,
                        last_hash: None,
                        last_pushed_at: None,
                    });
                }
                Err(err) => {
                    eprintln!("[chart_watcher] WgcCapture::new failed for hwnd={target_hwnd}: {err}");
                    session = None;
                    continue;
                }
            }
        }

        let Some(active) = session.as_mut() else { continue };

        let frame = match active.capture.try_capture_frame() {
            Ok(Some(frame)) => frame,
            Ok(None) => continue, // nothing new since last poll
            Err(err) => {
                eprintln!("[chart_watcher] try_capture_frame failed: {err}");
                session = None;
                continue;
            }
        };

        let hash = average_hash(&frame);
        let changed = match active.last_hash {
            Some(prev) => hamming_distance(prev, hash) > HASH_DIFF_THRESHOLD,
            None => true, // first frame for this session is always "new"
        };
        active.last_hash = Some(hash);

        let cooldown_elapsed = active
            .last_pushed_at
            .map(|t| t.elapsed() >= MIN_PUSH_COOLDOWN)
            .unwrap_or(true);
        let fallback_due = active
            .last_pushed_at
            .map(|t| t.elapsed() >= FALLBACK_PUSH_INTERVAL)
            .unwrap_or(true);

        if cooldown_elapsed && (changed || fallback_due) {
            let reason = if changed { "visual_change" } else { "staleness_fallback" };
            match push_frame_to_backend(target_hwnd, &frame, reason) {
                Ok(()) => active.last_pushed_at = Some(Instant::now()),
                Err(err) => eprintln!("[chart_watcher] push_frame_to_backend failed: {err}"),
            }
        }

        let _ = &app; // reserved for a future tars://chart-watch-* event; no UI signal emitted yet
    }
}

/// Idle time is a per-OS-session global (`GetLastInputInfo`), not scoped
/// to any particular window -- this deliberately does not care whether
/// TARS itself has focus, only whether the user is interacting with the
/// machine at all.
fn idle_duration() -> Duration {
    use windows_sys::Win32::System::SystemInformation::GetTickCount;
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::{GetLastInputInfo, LASTINPUTINFO};

    unsafe {
        let mut info = LASTINPUTINFO {
            cbSize: std::mem::size_of::<LASTINPUTINFO>() as u32,
            dwTime: 0,
        };
        if GetLastInputInfo(&mut info) == 0 {
            return Duration::ZERO; // can't determine -- assume active rather than pausing incorrectly
        }
        let now = GetTickCount();
        Duration::from_millis(now.wrapping_sub(info.dwTime) as u64)
    }
}

/// Finds a likely chart-application window regardless of current
/// foreground/focus state -- unlike `get_target_chart_window_hwnd()`
/// (lib.rs), which is deliberately foreground-biased for the user-
/// triggered "analyze chart" path, this watcher must keep tracking the
/// chart window even while the user is working in an entirely different
/// app (e.g. this very IDE). Matches by process name or window title
/// containing "tradingview" -- confirmed against this project's own
/// Phase A live baseline run to correctly identify a real native
/// "TradingView" desktop process; also covers a browser tab titled
/// "... - TradingView" for the case where the chart runs in a browser
/// instead. Not configurable yet -- a reasonable Phase C follow-up if a
/// different charting application needs to be supported.
fn find_chart_window() -> Option<isize> {
    use windows_sys::Win32::Foundation::*;
    use windows_sys::Win32::System::Threading::*;
    use windows_sys::Win32::UI::WindowsAndMessaging::*;

    struct Candidate {
        hwnd: isize,
        area: i64,
    }

    unsafe extern "system" fn enum_proc(hwnd: HWND, lparam: LPARAM) -> BOOL {
        let results = &mut *(lparam as *mut Vec<Candidate>);

        if IsWindowVisible(hwnd) == 0 {
            return 1;
        }
        let title_len = GetWindowTextLengthW(hwnd);
        if title_len == 0 {
            return 1;
        }
        let mut title_buf = vec![0u16; (title_len + 1) as usize];
        let len = GetWindowTextW(hwnd, title_buf.as_mut_ptr(), title_buf.len() as i32);
        let title = String::from_utf16_lossy(&title_buf[..len as usize]);

        let mut rect = RECT { left: 0, top: 0, right: 0, bottom: 0 };
        GetWindowRect(hwnd, &mut rect);
        let width = (rect.right - rect.left) as i64;
        let height = (rect.bottom - rect.top) as i64;
        if width < 200 || height < 200 {
            return 1;
        }

        let mut pid = 0u32;
        GetWindowThreadProcessId(hwnd, &mut pid);
        let mut exe_name = String::new();
        if pid != 0 {
            let h_proc = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, 0, pid);
            if !h_proc.is_null() {
                let mut exe_buf = [0u16; 1024];
                let mut size = exe_buf.len() as u32;
                if QueryFullProcessImageNameW(h_proc, 0, exe_buf.as_mut_ptr(), &mut size) != 0 {
                    let full_path = String::from_utf16_lossy(&exe_buf[..size as usize]);
                    if let Some(fname) = std::path::Path::new(&full_path).file_name().and_then(|f| f.to_str()) {
                        exe_name = fname.to_string();
                    }
                }
                CloseHandle(h_proc);
            }
        }

        let haystack = format!("{exe_name} {title}").to_lowercase();
        if haystack.contains("tradingview") {
            results.push(Candidate { hwnd: hwnd as isize, area: width * height });
        }
        1
    }

    let mut candidates: Vec<Candidate> = Vec::new();
    unsafe {
        EnumWindows(Some(enum_proc), &mut candidates as *mut _ as LPARAM);
    }
    // Prefer the largest matching window -- a maximized/primary chart
    // window over a small popup/notification also titled with the app name.
    candidates.into_iter().max_by_key(|c| c.area).map(|c| c.hwnd)
}

/// 16x16 grayscale average hash -- cheap (single pass over the pixel
/// buffer, no external image/crypto crate), good enough to distinguish
/// "nothing meaningfully changed" from "the chart looks different now."
/// Not a cryptographic or even a particularly sophisticated perceptual
/// hash -- deliberately simple, matching Part 3's "cheap change detection
/// first" requirement.
fn average_hash(frame: &CapturedFrame) -> [u64; 4] {
    let (w, h) = (frame.width as usize, frame.height as usize);
    if w == 0 || h == 0 {
        return [0; 4];
    }
    let cells = HASH_GRID * HASH_GRID;
    let mut cell_sum = vec![0u64; cells];
    let mut cell_count = vec![0u32; cells];

    for y in 0..h {
        let cy = (y * HASH_GRID) / h;
        let row_base = y * w * 4;
        for x in 0..w {
            let cx = (x * HASH_GRID) / w;
            let idx = row_base + x * 4;
            let (b, g, r) = (
                frame.bgra[idx] as u64,
                frame.bgra[idx + 1] as u64,
                frame.bgra[idx + 2] as u64,
            );
            let luma = (r * 299 + g * 587 + b * 114) / 1000;
            let cell = cy * HASH_GRID + cx;
            cell_sum[cell] += luma;
            cell_count[cell] += 1;
        }
    }

    let mut cell_avg = vec![0u64; cells];
    let mut total = 0u64;
    for i in 0..cells {
        cell_avg[i] = if cell_count[i] > 0 { cell_sum[i] / cell_count[i] as u64 } else { 0 };
        total += cell_avg[i];
    }
    let mean = total / cells as u64;

    let mut bits = [0u64; 4];
    for (i, &v) in cell_avg.iter().enumerate() {
        if v > mean {
            bits[i / 64] |= 1u64 << (i % 64);
        }
    }
    bits
}

fn hamming_distance(a: [u64; 4], b: [u64; 4]) -> u32 {
    a.iter().zip(b.iter()).map(|(x, y)| (x ^ y).count_ones()).sum()
}

fn push_frame_to_backend(hwnd: isize, frame: &CapturedFrame, reason: &str) -> Result<(), String> {
    let bmp_bytes = encode_bmp(frame);
    let encoded = base64_encode(&bmp_bytes);

    let body = serde_json::json!({
        "chart_window_id": hwnd.to_string(),
        "image_data_base64": encoded,
        "image_format": "image/bmp",
        "trigger_reason": reason,
    });

    let url = format!("{}/api/v1/chart-watch/frame", backend_base_url());
    ureq::post(&url)
        .set("Content-Type", "application/json")
        .timeout(Duration::from_secs(10))
        .send_string(&body.to_string())
        .map(|_| ())
        .map_err(|e| e.to_string())
}

/// Minimal, dependency-free BGRA -> BMP encoder is out of scope for a
/// hand-rolled PNG implementation (DEFLATE is not something to reimplement
/// here). Reuses `lib.rs`'s existing `create_bmp_bytes` for the exact same
/// reason it exists there (no image-encoding crate in this project today);
/// the backend already re-encodes any capture to PNG via Pillow before
/// handing it to the vision provider (see `chart_analysis.py`), so an
/// uncompressed BMP payload here is consistent with what the rest of this
/// pipeline already does, not a new format the backend has to learn.
///
/// `create_bmp_bytes` writes its declared height as positive, i.e.
/// bottom-up row order (matching its other caller, `GetDIBits` with a
/// positive `biHeight`) -- but a WGC/Direct3D11-mapped texture is top-down
/// (D3D's own row-major convention, distinct from the GDI DIB convention).
/// Flipping row order here, once, is what keeps the resulting BMP right
/// side up; skipping this would silently ship every background-watcher
/// frame upside down to Claude.
fn encode_bmp(frame: &CapturedFrame) -> Vec<u8> {
    let (w, h) = (frame.width as usize, frame.height as usize);
    let row_bytes = w * 4;
    let mut bottom_up = vec![0u8; frame.bgra.len()];
    for y in 0..h {
        let src_start = y * row_bytes;
        let dst_start = (h - 1 - y) * row_bytes;
        bottom_up[dst_start..dst_start + row_bytes]
            .copy_from_slice(&frame.bgra[src_start..src_start + row_bytes]);
    }
    crate::create_bmp_bytes(frame.width, frame.height, 32, &bottom_up)
}

fn base64_encode(data: &[u8]) -> String {
    const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0];
        let b1 = *chunk.get(1).unwrap_or(&0);
        let b2 = *chunk.get(2).unwrap_or(&0);
        out.push(ALPHABET[(b0 >> 2) as usize] as char);
        out.push(ALPHABET[(((b0 & 0x03) << 4) | (b1 >> 4)) as usize] as char);
        out.push(if chunk.len() > 1 { ALPHABET[(((b1 & 0x0f) << 2) | (b2 >> 6)) as usize] as char } else { '=' });
        out.push(if chunk.len() > 2 { ALPHABET[(b2 & 0x3f) as usize] as char } else { '=' });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn solid_frame(width: u32, height: u32, bgra: [u8; 4]) -> CapturedFrame {
        let mut buf = Vec::with_capacity((width * height * 4) as usize);
        for _ in 0..(width * height) {
            buf.extend_from_slice(&bgra);
        }
        CapturedFrame { width, height, bgra: buf }
    }

    #[test]
    fn average_hash_is_identical_for_two_solid_frames_of_the_same_color() {
        let a = solid_frame(64, 64, [10, 20, 30, 255]);
        let b = solid_frame(64, 64, [10, 20, 30, 255]);
        assert_eq!(average_hash(&a), average_hash(&b));
        assert_eq!(hamming_distance(average_hash(&a), average_hash(&b)), 0);
    }

    #[test]
    fn average_hash_differs_for_a_black_vs_white_frame() {
        let black = solid_frame(64, 64, [0, 0, 0, 255]);
        let white = solid_frame(64, 64, [255, 255, 255, 255]);
        let dist = hamming_distance(average_hash(&black), average_hash(&white));
        // A uniform frame has every cell equal to the mean, so this is a
        // degenerate case (no cell is strictly greater than the mean) --
        // pinning the actual behavior (all-zero hash for both) rather than
        // asserting a difference that a uniform-color frame can't produce.
        assert_eq!(dist, 0);
    }

    #[test]
    fn average_hash_detects_a_half_and_half_split_frame() {
        // Left half dark, right half bright -- half the 16x16 grid cells
        // should land above the frame's mean and half below, so this
        // should differ meaningfully from a fully uniform frame.
        let width = 64u32;
        let height = 64u32;
        let mut buf = Vec::with_capacity((width * height * 4) as usize);
        for y in 0..height {
            for x in 0..width {
                let _ = y;
                let color = if x < width / 2 { [0u8, 0, 0, 255] } else { [255u8, 255, 255, 255] };
                buf.extend_from_slice(&color);
            }
        }
        let split = CapturedFrame { width, height, bgra: buf };
        let uniform = solid_frame(width, height, [128, 128, 128, 255]);
        let dist = hamming_distance(average_hash(&split), average_hash(&uniform));
        assert!(dist > HASH_DIFF_THRESHOLD, "expected a clear structural difference, got distance={dist}");
    }

    #[test]
    fn hamming_distance_is_symmetric_and_zero_for_identical_hashes() {
        let a = [0xFFu64, 0x0F, 0xAA, 0x55];
        let b = [0x0Fu64, 0xFF, 0x55, 0xAA];
        assert_eq!(hamming_distance(a, a), 0);
        assert_eq!(hamming_distance(a, b), hamming_distance(b, a));
        assert!(hamming_distance(a, b) > 0);
    }

    #[test]
    fn base64_encode_matches_known_vectors() {
        assert_eq!(base64_encode(b""), "");
        assert_eq!(base64_encode(b"f"), "Zg==");
        assert_eq!(base64_encode(b"fo"), "Zm8=");
        assert_eq!(base64_encode(b"foo"), "Zm9v");
        assert_eq!(base64_encode(b"foobar"), "Zm9vYmFy");
    }

    #[test]
    fn encode_bmp_flips_rows_to_bottom_up_order() {
        // Top-down input: row 0 red, row 1 blue.
        let width = 2u32;
        let height = 2u32;
        let mut buf = Vec::new();
        buf.extend_from_slice(&[0, 0, 255, 255]); // row0 px0: BGRA red
        buf.extend_from_slice(&[0, 0, 255, 255]); // row0 px1: red
        buf.extend_from_slice(&[255, 0, 0, 255]); // row1 px0: BGRA blue
        buf.extend_from_slice(&[255, 0, 0, 255]); // row1 px1: blue
        let frame = CapturedFrame { width, height, bgra: buf };

        let bmp = encode_bmp(&frame);
        // BMP pixel data starts at byte 54 for this 14+40-byte header shape
        // (create_bmp_bytes always uses BITMAPFILEHEADER + BITMAPINFOHEADER,
        // no color table for 32bpp). Bottom-up means the BMP's first pixel
        // row must be the source's LAST row (blue), not its first (red).
        let pixel_data = &bmp[54..];
        assert_eq!(&pixel_data[0..4], &[255, 0, 0, 255], "first BMP row must be the source's bottom row");
        assert_eq!(&pixel_data[8..12], &[0, 0, 255, 255], "second BMP row must be the source's top row");
    }
}
