use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Emitter, Manager};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};

mod wake_engine;
#[cfg(target_os = "windows")]
mod capture_wgc;
#[cfg(target_os = "windows")]
mod chart_watcher;

static CAPTURE_COUNTER: AtomicUsize = AtomicUsize::new(1);
static LAST_EXTERNAL_HWND: std::sync::atomic::AtomicIsize = std::sync::atomic::AtomicIsize::new(0);

#[cfg(target_os = "windows")]
unsafe fn get_target_chart_window_hwnd() -> windows_sys::Win32::Foundation::HWND {
    use windows_sys::Win32::Foundation::*;
    use windows_sys::Win32::System::Threading::*;
    use windows_sys::Win32::UI::WindowsAndMessaging::*;

    let current_pid = GetCurrentProcessId();
    let current_fg = GetForegroundWindow();

    // 1. If current foreground is an external application (e.g. TradingView), preserve & return it
    if !current_fg.is_null() {
        let mut fg_pid = 0u32;
        GetWindowThreadProcessId(current_fg, &mut fg_pid);
        if fg_pid != 0 && fg_pid != current_pid {
            LAST_EXTERNAL_HWND.store(current_fg as isize, Ordering::SeqCst);
            return current_fg;
        }
    }

    // 2. If TARS is foreground, check the preserved previous external window
    let stored_raw = LAST_EXTERNAL_HWND.load(Ordering::SeqCst);
    if stored_raw != 0 {
        let stored_hwnd = stored_raw as HWND;
        if IsWindow(stored_hwnd) != 0 && IsWindowVisible(stored_hwnd) != 0 {
            let mut pid = 0u32;
            GetWindowThreadProcessId(stored_hwnd, &mut pid);
            if pid != 0 && pid != current_pid {
                return stored_hwnd;
            }
        }
    }

    // 3. Fallback: Search top-level windows in Z-order for top visible non-TARS application
    let mut curr = GetTopWindow(std::ptr::null_mut());
    while !curr.is_null() {
        if IsWindowVisible(curr) != 0 {
            let mut pid = 0u32;
            GetWindowThreadProcessId(curr, &mut pid);
            if pid != 0 && pid != current_pid {
                let mut title_buf = [0u16; 64];
                let len = GetWindowTextW(curr, title_buf.as_mut_ptr(), 64);
                let mut rect = RECT { left: 0, top: 0, right: 0, bottom: 0 };
                GetWindowRect(curr, &mut rect);
                let w = rect.right - rect.left;
                let h = rect.bottom - rect.top;
                if len > 0 && w > 200 && h > 200 {
                    LAST_EXTERNAL_HWND.store(curr as isize, Ordering::SeqCst);
                    return curr;
                }
            }
        }
        curr = GetWindow(curr, GW_HWNDNEXT);
    }

    current_fg
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WindowBounds {
    pub x: i32,
    pub y: i32,
    pub width: i32,
    pub height: i32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ActiveWindowContext {
    pub executable: String,
    pub process_id: Option<u32>,
    pub window_title: String,
    pub window_bounds: Option<WindowBounds>,
    pub captured_at: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MonitorInfo {
    pub id: String,
    pub name: String,
    pub is_primary: bool,
    pub bounds: WindowBounds,
    pub work_area: WindowBounds,
    pub scale_factor: f64,
    pub dpi: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScreenCaptureResult {
    pub capture_id: String,
    pub captured_at: String,
    pub source: String, // "active_window" | "region" | "monitor"
    pub executable: String,
    pub window_title: String,
    pub bounds: WindowBounds,
    pub scale_factor: f64,
    pub dpi: u32,
    pub width: u32,
    pub height: u32,
    pub is_secure_desktop: bool,
    pub image_format: String,
    pub image_data_base64: Option<String>,
    pub temp_file_path: Option<String>,
    pub error: Option<String>,
    // The captured window's own HWND, as a string -- present only for
    // "active_window" captures with a real target (None for the no-window,
    // region, and monitor cases). Lets the backend look up HotChartState
    // for this exact window using the SAME chart_window_id scheme
    // chart_watcher.rs already uses (hwnd.to_string()) -- see TARS
    // Alexa-Speed Phase D. Additive field; existing consumers that ignore
    // unknown JSON fields are unaffected.
    pub window_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UIElementNode {
    pub id: String,
    pub name: String,
    pub role: String,
    pub class_name: String,
    pub bounds: Option<WindowBounds>,
    pub is_enabled: bool,
    pub is_visible: bool,
    pub children: Vec<UIElementNode>,
}

// Simple standard Base64 encoder (RFC 4648) without external dependency overhead
fn encode_base64(data: &[u8]) -> String {
    const CHARSET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut result = String::with_capacity((data.len() + 2) / 3 * 4);
    let mut i = 0;
    while i < data.len() {
        let b0 = data[i];
        let b1 = if i + 1 < data.len() { data[i + 1] } else { 0 };
        let b2 = if i + 2 < data.len() { data[i + 2] } else { 0 };

        let triple = ((b0 as u32) << 16) | ((b1 as u32) << 8) | (b2 as u32);

        result.push(CHARSET[((triple >> 18) & 0x3F) as usize] as char);
        result.push(CHARSET[((triple >> 12) & 0x3F) as usize] as char);

        if i + 1 < data.len() {
            result.push(CHARSET[((triple >> 6) & 0x3F) as usize] as char);
        } else {
            result.push('=');
        }

        if i + 2 < data.len() {
            result.push(CHARSET[(triple & 0x3F) as usize] as char);
        } else {
            result.push('=');
        }

        i += 3;
    }
    result
}

// Constructs standard uncompressed 24-bit/32-bit BMP bytes from raw BGRA/BGR pixel buffer
fn create_bmp_bytes(width: u32, height: u32, bits_per_pixel: u16, pixel_data: &[u8]) -> Vec<u8> {
    let row_size = ((width * bits_per_pixel as u32 + 31) / 32) * 4;
    let image_size = row_size * height;
    let file_header_size = 14u32;
    let info_header_size = 40u32;
    let data_offset = file_header_size + info_header_size;
    let total_file_size = data_offset + image_size;

    let mut bmp = Vec::with_capacity(total_file_size as usize);

    // BITMAPFILEHEADER (14 bytes)
    bmp.extend_from_slice(b"BM"); // Type
    bmp.extend_from_slice(&total_file_size.to_le_bytes()); // Size
    bmp.extend_from_slice(&0u16.to_le_bytes()); // Reserved 1
    bmp.extend_from_slice(&0u16.to_le_bytes()); // Reserved 2
    bmp.extend_from_slice(&data_offset.to_le_bytes()); // OffBits

    // BITMAPINFOHEADER (40 bytes)
    bmp.extend_from_slice(&info_header_size.to_le_bytes()); // Size
    bmp.extend_from_slice(&(width as i32).to_le_bytes()); // Width
    bmp.extend_from_slice(&(height as i32).to_le_bytes()); // Height (bottom-up if positive)
    bmp.extend_from_slice(&1u16.to_le_bytes()); // Planes
    bmp.extend_from_slice(&bits_per_pixel.to_le_bytes()); // BitCount
    bmp.extend_from_slice(&0u32.to_le_bytes()); // Compression (BI_RGB = 0)
    bmp.extend_from_slice(&image_size.to_le_bytes()); // SizeImage
    bmp.extend_from_slice(&2835u32.to_le_bytes()); // XPelsPerMeter (~72 DPI)
    bmp.extend_from_slice(&2835u32.to_le_bytes()); // YPelsPerMeter (~72 DPI)
    bmp.extend_from_slice(&0u32.to_le_bytes()); // ClrUsed
    bmp.extend_from_slice(&0u32.to_le_bytes()); // ClrImportant

    bmp.extend_from_slice(pixel_data);
    bmp
}

// Bounded temporary directory for screenshots with automatic FIFO eviction (keep max 10)
fn get_temp_captures_dir() -> PathBuf {
    let mut dir = std::env::temp_dir();
    dir.push("tars_captures");
    let _ = fs::create_dir_all(&dir);
    dir
}

fn save_temp_capture(capture_id: &str, bmp_bytes: &[u8]) -> Result<String, String> {
    let dir = get_temp_captures_dir();
    let file_path = dir.join(format!("capture_{}.bmp", capture_id));
    fs::write(&file_path, bmp_bytes).map_err(|e| e.to_string())?;

    // Auto-clean: if more than 10 files in temp dir, remove oldest
    if let Ok(entries) = fs::read_dir(&dir) {
        let mut files: Vec<PathBuf> = entries
            .filter_map(|e| e.ok().map(|ent| ent.path()))
            .filter(|p| p.is_file() && p.extension().and_then(|x| x.to_str()) == Some("bmp"))
            .collect();
        if files.len() > 10 {
            files.sort_by_key(|p| fs::metadata(p).and_then(|m| m.modified()).ok());
            for old_file in files.iter().take(files.len() - 10) {
                let _ = fs::remove_file(old_file);
            }
        }
    }

    Ok(file_path.to_string_lossy().to_string())
}

fn is_secure_desktop_window(exe_name: &str, window_title: &str, class_name: &str) -> bool {
    let exe = exe_name.to_lowercase();
    let title = window_title.to_lowercase();
    let cls = class_name.to_lowercase();

    exe.contains("consent.exe")
        || exe.contains("winlogon.exe")
        || exe.contains("logonui.exe")
        || exe.contains("lockapp.exe")
        || exe.contains("credentialuibroker.exe")
        || title.contains("windows security")
        || title.contains("user account control")
        || cls.contains("credential dialog")
        || cls.contains("secure desktop")
}

#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! TARS Windows Native Assistant Ready.", name)
}

#[tauri::command]
fn mark_frontend_ready(app: tauri::AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("main") {
        window.set_title("TARS Ready").map_err(|error| error.to_string())?;
        return Ok(());
    }
    Err("Main window not found".into())
}

#[tauri::command]
fn is_always_on_top(app: tauri::AppHandle) -> Result<bool, String> {
    if let Some(window) = app.get_webview_window("main") {
        return window.is_always_on_top().map_err(|e| e.to_string());
    }
    Ok(false)
}

#[tauri::command]
fn toggle_compact_mode(app: tauri::AppHandle, is_compact: Option<bool>) -> Result<bool, String> {
    if let Some(window) = app.get_webview_window("main") {
        let next_compact = is_compact.unwrap_or_else(|| !window.is_always_on_top().unwrap_or(false));
        if next_compact {
            window.set_size(tauri::LogicalSize::new(380.0, 180.0)).map_err(|e| e.to_string())?;
            window.set_always_on_top(true).map_err(|e| e.to_string())?;
        } else {
            window.set_size(tauri::LogicalSize::new(1280.0, 840.0)).map_err(|e| e.to_string())?;
            window.set_always_on_top(false).map_err(|e| e.to_string())?;
        }
        return Ok(next_compact);
    }
    Ok(false)
}

#[tauri::command]
fn set_window_size(app: tauri::AppHandle, width: f64, height: f64, always_on_top: Option<bool>) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("main") {
        window.set_size(tauri::LogicalSize::new(width, height)).map_err(|e| e.to_string())?;
        if let Some(on_top) = always_on_top {
            window.set_always_on_top(on_top).map_err(|e| e.to_string())?;
        }
        return Ok(());
    }
    Err("Main window not found".into())
}

#[tauri::command]
fn summon_hud(app: tauri::AppHandle, mode: Option<String>) -> Result<(), String> {
    summon_hud_impl(&app, mode.as_deref())
}

#[tauri::command]
fn hide_hud(app: tauri::AppHandle) -> Result<(), String> {
    hide_hud_impl(&app)
}

#[tauri::command]
fn toggle_hud(app: tauri::AppHandle, mode: Option<String>) -> Result<bool, String> {
    toggle_hud_impl(&app, mode.as_deref())
}

#[tauri::command]
fn exit_app(app: tauri::AppHandle) -> Result<(), String> {
    app.exit(0);
    Ok(())
}

#[derive(Debug, Clone, Serialize)]
pub struct WakeEngineStatus {
    pub running: bool,
    pub last_error: Option<String>,
}

#[tauri::command]
fn wake_engine_status() -> WakeEngineStatus {
    WakeEngineStatus {
        running: wake_engine::is_running(),
        last_error: wake_engine::last_error(),
    }
}

#[tauri::command]
fn set_wake_playback_state(app: tauri::AppHandle, speaking: bool) -> Result<(), String> {
    wake_engine::set_playback_speaking(speaking, &app);
    Ok(())
}

fn summon_hud_impl(app: &tauri::AppHandle, mode: Option<&str>) -> Result<(), String> {
    #[cfg(target_os = "windows")]
    unsafe {
        use windows_sys::Win32::System::Threading::*;
        use windows_sys::Win32::UI::WindowsAndMessaging::*;
        let fg = GetForegroundWindow();
        if !fg.is_null() {
            let mut pid = 0u32;
            GetWindowThreadProcessId(fg, &mut pid);
            if pid != 0 && pid != GetCurrentProcessId() {
                LAST_EXTERNAL_HWND.store(fg as isize, Ordering::SeqCst);
            }
        }
    }

    if let Some(window) = app.get_webview_window("main") {
        window.show().map_err(|e| e.to_string())?;
        window.unminimize().map_err(|e| e.to_string())?;
        let requested_mode = mode.unwrap_or("voice");
        let (width, height, always_on_top) = if requested_mode == "voice" {
            (420.0, 260.0, true)
        } else {
            (1100.0, 780.0, false)
        };
        window.set_size(tauri::LogicalSize::new(width, height)).map_err(|e| e.to_string())?;
        window.set_always_on_top(always_on_top).map_err(|e| e.to_string())?;
        window.set_focus().map_err(|e| e.to_string())?;
        let _ = app.emit("tars://summon-hud", requested_mode);
        return Ok(());
    }
    Err("Main window not found".into())
}

fn show_main_impl(app: &tauri::AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("main") {
        window.show().map_err(|e| e.to_string())?;
        window.unminimize().map_err(|e| e.to_string())?;
        window.set_size(tauri::LogicalSize::new(1100.0, 780.0)).map_err(|e| e.to_string())?;
        window.set_always_on_top(false).map_err(|e| e.to_string())?;
        window.set_focus().map_err(|e| e.to_string())?;
        let _ = app.emit("tars://summon-hud", "workstation");
        return Ok(());
    }
    Err("Main window not found".into())
}

fn hide_hud_impl(app: &tauri::AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("main") {
        window.hide().map_err(|e| e.to_string())?;
        return Ok(());
    }
    Err("Main window not found".into())
}

fn toggle_hud_impl(app: &tauri::AppHandle, mode: Option<&str>) -> Result<bool, String> {
    if let Some(window) = app.get_webview_window("main") {
        let is_visible = window.is_visible().unwrap_or(false);
        if is_visible {
            let _ = hide_hud_impl(app);
            Ok(false)
        } else {
            let _ = summon_hud_impl(app, mode);
            Ok(true)
        }
    } else {
        Err("Main window not found".into())
    }
}

#[tauri::command]
fn get_active_window_context() -> Result<ActiveWindowContext, String> {
    #[cfg(target_os = "windows")]
    {
        use windows_sys::Win32::Foundation::*;
        use windows_sys::Win32::System::Threading::*;
        use windows_sys::Win32::UI::WindowsAndMessaging::*;

        unsafe {
            let hwnd = get_target_chart_window_hwnd();
            if hwnd.is_null() {
                return Ok(ActiveWindowContext {
                    executable: "unknown.exe".into(),
                    process_id: None,
                    window_title: "Desktop Session".into(),
                    window_bounds: None,
                    captured_at: Some(chrono::Utc::now().to_rfc3339()),
                });
            }

            // Capture Window Title
            let mut title_buf = [0u16; 512];
            let len = GetWindowTextW(hwnd, title_buf.as_mut_ptr(), 512);
            let window_title = if len > 0 {
                String::from_utf16_lossy(&title_buf[..len as usize])
            } else {
                String::new()
            };

            // Capture Process ID
            let mut pid = 0u32;
            GetWindowThreadProcessId(hwnd, &mut pid);

            // Capture Executable Name (basename only per contract)
            let mut exe_name = "unknown.exe".to_string();
            if pid != 0 {
                let h_process = OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ,
                    0,
                    pid,
                );
                if !h_process.is_null() {
                    let mut exe_buf = [0u16; 1024];
                    let mut size = exe_buf.len() as u32;
                    if QueryFullProcessImageNameW(
                        h_process,
                        0,
                        exe_buf.as_mut_ptr(),
                        &mut size,
                    ) != 0
                    {
                        let full_path = String::from_utf16_lossy(&exe_buf[..size as usize]);
                        if let Some(filename) = std::path::Path::new(&full_path)
                            .file_name()
                            .and_then(|f| f.to_str())
                        {
                            exe_name = filename.to_string();
                        }
                    }
                    CloseHandle(h_process);
                }
            }

            // Capture Screen Bounds
            let mut rect = RECT {
                left: 0,
                top: 0,
                right: 0,
                bottom: 0,
            };
            let bounds = if GetWindowRect(hwnd, &mut rect) != 0 {
                Some(WindowBounds {
                    x: rect.left,
                    y: rect.top,
                    width: (rect.right - rect.left).max(0),
                    height: (rect.bottom - rect.top).max(0),
                })
            } else {
                None
            };

            Ok(ActiveWindowContext {
                executable: exe_name,
                process_id: if pid != 0 { Some(pid) } else { None },
                window_title,
                window_bounds: bounds,
                captured_at: Some(chrono::Utc::now().to_rfc3339()),
            })
        }
    }
    #[cfg(not(target_os = "windows"))]
    {
        Ok(ActiveWindowContext {
            executable: "desktop.exe".into(),
            process_id: None,
            window_title: "Active Window".into(),
            window_bounds: None,
            captured_at: Some(chrono::Utc::now().to_rfc3339()),
        })
    }
}

#[tauri::command]
fn get_monitors_geometry() -> Result<Vec<MonitorInfo>, String> {
    #[cfg(target_os = "windows")]
    {
        use windows_sys::Win32::UI::HiDpi::*;
        use windows_sys::Win32::UI::WindowsAndMessaging::*;

        unsafe {
            let primary_width = GetSystemMetrics(SM_CXSCREEN);
            let primary_height = GetSystemMetrics(SM_CYSCREEN);
            let virtual_x = GetSystemMetrics(SM_XVIRTUALSCREEN);
            let virtual_y = GetSystemMetrics(SM_YVIRTUALSCREEN);
            let virtual_width = GetSystemMetrics(SM_CXVIRTUALSCREEN);
            let virtual_height = GetSystemMetrics(SM_CYVIRTUALSCREEN);
            let monitor_count = GetSystemMetrics(SM_CMONITORS).max(1);

            let dpi = GetDpiForSystem();
            let scale = dpi as f64 / 96.0;

            let mut monitors = Vec::new();

            // Primary display
            monitors.push(MonitorInfo {
                id: "DISPLAY_PRIMARY".into(),
                name: "Primary Monitor".into(),
                is_primary: true,
                bounds: WindowBounds {
                    x: 0,
                    y: 0,
                    width: primary_width,
                    height: primary_height,
                },
                work_area: WindowBounds {
                    x: 0,
                    y: 0,
                    width: primary_width,
                    height: primary_height.saturating_sub(40), // taskbar allowance
                },
                scale_factor: scale,
                dpi,
            });

            // If multi-monitor virtual screen is larger, also report virtual desktop monitor
            if monitor_count > 1 && (virtual_width != primary_width || virtual_height != primary_height) {
                monitors.push(MonitorInfo {
                    id: "DISPLAY_VIRTUAL_DESKTOP".into(),
                    name: "Virtual Desktop Span".into(),
                    is_primary: false,
                    bounds: WindowBounds {
                        x: virtual_x,
                        y: virtual_y,
                        width: virtual_width,
                        height: virtual_height,
                    },
                    work_area: WindowBounds {
                        x: virtual_x,
                        y: virtual_y,
                        width: virtual_width,
                        height: virtual_height,
                    },
                    scale_factor: scale,
                    dpi,
                });
            }

            Ok(monitors)
        }
    }
    #[cfg(not(target_os = "windows"))]
    {
        Ok(vec![MonitorInfo {
            id: "DISPLAY1".into(),
            name: "Default Display".into(),
            is_primary: true,
            bounds: WindowBounds { x: 0, y: 0, width: 1920, height: 1080 },
            work_area: WindowBounds { x: 0, y: 0, width: 1920, height: 1040 },
            scale_factor: 1.0,
            dpi: 96,
        }])
    }
}

/// Chart-analysis capture: TARS's own window (shown with "Looking at the
/// chart..." right before this runs) sits directly over the target chart
/// window's screen region, and `capture_active_window` grabs raw on-screen
/// pixels (BitBlt from the screen DC) rather than the target window's own
/// contents -- so without this, the capture contains TARS itself, not the
/// chart underneath it. Hides TARS, gives DWM a moment to repaint whatever
/// was underneath, captures, then restores TARS exactly as it was.
#[tauri::command]
fn capture_chart_window(
    app: tauri::AppHandle,
    include_image_data: Option<bool>,
) -> Result<ScreenCaptureResult, String> {
    // Perf instrumentation (TARS MASTER MILESTONE Phase 2): stderr so it
    // shows up in a redirected launch log without touching the JSON
    // response shape the frontend depends on.
    let t_start = std::time::Instant::now();
    let window = app.get_webview_window("main");
    let was_visible = window
        .as_ref()
        .map(|w| w.is_visible().unwrap_or(false))
        .unwrap_or(false);

    if was_visible {
        if let Some(w) = &window {
            let _ = w.hide();
        }
    }
    let t_hidden = t_start.elapsed().as_millis();

    // Let DWM finish repainting the window(s) now exposed underneath
    // before grabbing screen pixels.
    std::thread::sleep(std::time::Duration::from_millis(220));
    let t_dwm_wait_done = t_start.elapsed().as_millis();

    let result = capture_active_window(include_image_data);
    let t_capture_done = t_start.elapsed().as_millis();

    if was_visible {
        if let Some(w) = &window {
            let _ = w.show();
            let _ = w.set_focus();
        }
    }
    let t_restored = t_start.elapsed().as_millis();

    eprintln!(
        "[PERF][capture_chart_window] hide={}ms dwm_wait_done={}ms capture_done={}ms (capture_only={}ms) restored={}ms",
        t_hidden,
        t_dwm_wait_done,
        t_capture_done,
        t_capture_done - t_dwm_wait_done,
        t_restored
    );

    match &result {
        Ok(capture) if capture.executable.eq_ignore_ascii_case("tars-companion.exe") => Err(
            "Captured window is TARS itself -- hiding before capture did not \
             clear it from the target region, refusing to send a self-capture \
             to the model."
                .to_string(),
        ),
        _ => result,
    }
}

#[tauri::command]
fn capture_active_window(include_image_data: Option<bool>) -> Result<ScreenCaptureResult, String> {
    #[cfg(target_os = "windows")]
    {
        use windows_sys::Win32::Foundation::*;
        use windows_sys::Win32::Graphics::Gdi::*;
        use windows_sys::Win32::System::Threading::*;
        use windows_sys::Win32::UI::HiDpi::*;
        use windows_sys::Win32::UI::WindowsAndMessaging::*;

        unsafe {
            let hwnd = get_target_chart_window_hwnd();
            let now = chrono::Utc::now().to_rfc3339();
            let count = CAPTURE_COUNTER.fetch_add(1, Ordering::SeqCst);
            let capture_id = format!("cap_{}_{}", chrono::Utc::now().timestamp_millis(), count);

            if hwnd.is_null() {
                return Ok(ScreenCaptureResult {
                    capture_id,
                    captured_at: now,
                    source: "active_window".into(),
                    executable: "unknown.exe".into(),
                    window_title: "No Foreground Window".into(),
                    bounds: WindowBounds { x: 0, y: 0, width: 0, height: 0 },
                    scale_factor: 1.0,
                    dpi: 96,
                    width: 0,
                    height: 0,
                    window_id: None,
                    is_secure_desktop: false,
                    image_format: "image/bmp".into(),
                    image_data_base64: None,
                    temp_file_path: None,
                    error: Some("No foreground window active".into()),
                });
            }

            // Window title
            let mut title_buf = [0u16; 512];
            let len = GetWindowTextW(hwnd, title_buf.as_mut_ptr(), 512);
            let window_title = if len > 0 {
                String::from_utf16_lossy(&title_buf[..len as usize])
            } else {
                String::new()
            };

            // Window class
            let mut class_buf = [0u16; 256];
            let class_len = GetClassNameW(hwnd, class_buf.as_mut_ptr(), 256);
            let class_name = if class_len > 0 {
                String::from_utf16_lossy(&class_buf[..class_len as usize])
            } else {
                String::new()
            };

            // Process info
            let mut pid = 0u32;
            GetWindowThreadProcessId(hwnd, &mut pid);
            let mut exe_name = "unknown.exe".to_string();
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

            // Secure Desktop Protection (Never capture secure desktops, UAC, LockApp)
            if is_secure_desktop_window(&exe_name, &window_title, &class_name) {
                return Ok(ScreenCaptureResult {
                    capture_id,
                    captured_at: now,
                    source: "active_window".into(),
                    executable: exe_name,
                    window_title,
                    bounds: WindowBounds { x: 0, y: 0, width: 0, height: 0 },
                    scale_factor: 1.0,
                    dpi: 96,
                    width: 0,
                    height: 0,
                    window_id: None,
                    is_secure_desktop: true,
                    image_format: "image/bmp".into(),
                    image_data_base64: None,
                    temp_file_path: None,
                    error: Some("Capture refused: Secure desktop or credential screen active".into()),
                });
            }

            // Window Rect
            let mut rect = RECT { left: 0, top: 0, right: 0, bottom: 0 };
            GetWindowRect(hwnd, &mut rect);
            let w = (rect.right - rect.left).max(1);
            let h = (rect.bottom - rect.top).max(1);

            let dpi = GetDpiForWindow(hwnd);
            let scale_factor = if dpi > 0 { dpi as f64 / 96.0 } else { 1.0 };

            // GDI capture
            let hdc_screen = GetDC(std::ptr::null_mut());
            let hdc_mem = CreateCompatibleDC(hdc_screen);
            let hbm = CreateCompatibleBitmap(hdc_screen, w, h);
            let old_bm = SelectObject(hdc_mem, hbm);

            BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, rect.left, rect.top, SRCCOPY | CAPTUREBLT);

            let mut bmi = BITMAPINFO {
                bmiHeader: BITMAPINFOHEADER {
                    biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                    biWidth: w,
                    biHeight: h, // Bottom-up DIB
                    biPlanes: 1,
                    biBitCount: 32,
                    biCompression: BI_RGB as u32,
                    biSizeImage: (w * h * 4) as u32,
                    biXPelsPerMeter: 2835,
                    biYPelsPerMeter: 2835,
                    biClrUsed: 0,
                    biClrImportant: 0,
                },
                bmiColors: [RGBQUAD { rgbBlue: 0, rgbGreen: 0, rgbRed: 0, rgbReserved: 0 }; 1],
            };

            let mut pixels = vec![0u8; (w * h * 4) as usize];
            GetDIBits(
                hdc_mem,
                hbm,
                0,
                h as u32,
                pixels.as_mut_ptr() as *mut _,
                &mut bmi,
                DIB_RGB_COLORS,
            );

            // Cleanup GDI objects
            SelectObject(hdc_mem, old_bm);
            DeleteObject(hbm);
            DeleteDC(hdc_mem);
            ReleaseDC(std::ptr::null_mut(), hdc_screen);

            let bmp_bytes = create_bmp_bytes(w as u32, h as u32, 32, &pixels);
            let temp_path = save_temp_capture(&capture_id, &bmp_bytes).ok();

            let base64_str = if include_image_data.unwrap_or(true) {
                Some(format!("data:image/bmp;base64,{}", encode_base64(&bmp_bytes)))
            } else {
                None
            };

            Ok(ScreenCaptureResult {
                capture_id,
                captured_at: now,
                source: "active_window".into(),
                executable: exe_name,
                window_title,
                bounds: WindowBounds {
                    x: rect.left,
                    y: rect.top,
                    width: w,
                    height: h,
                },
                scale_factor,
                dpi,
                width: w as u32,
                height: h as u32,
                window_id: Some((hwnd as isize).to_string()),
                is_secure_desktop: false,
                image_format: "image/bmp".into(),
                image_data_base64: base64_str,
                temp_file_path: temp_path,
                error: None,
            })
        }
    }
    #[cfg(not(target_os = "windows"))]
    {
        let count = CAPTURE_COUNTER.fetch_add(1, Ordering::SeqCst);
        let capture_id = format!("cap_mock_{}", count);
        Ok(ScreenCaptureResult {
            capture_id,
            captured_at: chrono::Utc::now().to_rfc3339(),
            source: "active_window".into(),
            window_id: None,
            executable: "mock_browser.exe".into(),
            window_title: "Mock Window".into(),
            bounds: WindowBounds { x: 100, y: 100, width: 800, height: 600 },
            scale_factor: 1.0,
            dpi: 96,
            width: 800,
            height: 600,
            is_secure_desktop: false,
            image_format: "image/bmp".into(),
            image_data_base64: Some("data:image/bmp;base64,Qk0AAAAAAAAAAAAAAA==".into()),
            temp_file_path: None,
            error: None,
        })
    }
}

#[tauri::command]
fn capture_screen_region(
    x: i32,
    y: i32,
    width: i32,
    height: i32,
    include_image_data: Option<bool>,
) -> Result<ScreenCaptureResult, String> {
    #[cfg(target_os = "windows")]
    {
        use windows_sys::Win32::Graphics::Gdi::*;
        use windows_sys::Win32::UI::HiDpi::*;

        unsafe {
            let now = chrono::Utc::now().to_rfc3339();
            let count = CAPTURE_COUNTER.fetch_add(1, Ordering::SeqCst);
            let capture_id = format!("region_{}_{}", chrono::Utc::now().timestamp_millis(), count);

            // Bounded dimension validation (prevent memory exhaustion)
            let w = width.clamp(1, 3840);
            let h = height.clamp(1, 2160);

            let dpi = GetDpiForSystem();
            let scale_factor = dpi as f64 / 96.0;

            let hdc_screen = GetDC(std::ptr::null_mut());
            let hdc_mem = CreateCompatibleDC(hdc_screen);
            let hbm = CreateCompatibleBitmap(hdc_screen, w, h);
            let old_bm = SelectObject(hdc_mem, hbm);

            BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, x, y, SRCCOPY | CAPTUREBLT);

            let mut bmi = BITMAPINFO {
                bmiHeader: BITMAPINFOHEADER {
                    biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                    biWidth: w,
                    biHeight: h,
                    biPlanes: 1,
                    biBitCount: 32,
                    biCompression: BI_RGB as u32,
                    biSizeImage: (w * h * 4) as u32,
                    biXPelsPerMeter: 2835,
                    biYPelsPerMeter: 2835,
                    biClrUsed: 0,
                    biClrImportant: 0,
                },
                bmiColors: [RGBQUAD { rgbBlue: 0, rgbGreen: 0, rgbRed: 0, rgbReserved: 0 }; 1],
            };

            let mut pixels = vec![0u8; (w * h * 4) as usize];
            GetDIBits(
                hdc_mem,
                hbm,
                0,
                h as u32,
                pixels.as_mut_ptr() as *mut _,
                &mut bmi,
                DIB_RGB_COLORS,
            );

            SelectObject(hdc_mem, old_bm);
            DeleteObject(hbm);
            DeleteDC(hdc_mem);
            ReleaseDC(std::ptr::null_mut(), hdc_screen);

            let bmp_bytes = create_bmp_bytes(w as u32, h as u32, 32, &pixels);
            let temp_path = save_temp_capture(&capture_id, &bmp_bytes).ok();

            let base64_str = if include_image_data.unwrap_or(true) {
                Some(format!("data:image/bmp;base64,{}", encode_base64(&bmp_bytes)))
            } else {
                None
            };

            Ok(ScreenCaptureResult {
                capture_id,
                captured_at: now,
                source: "region".into(),
                executable: "desktop_region".into(),
                window_title: format!("Region ({}, {}, {}x{})", x, y, w, h),
                bounds: WindowBounds { x, y, width: w, height: h },
                scale_factor,
                dpi,
                width: w as u32,
                height: h as u32,
                window_id: None,
                is_secure_desktop: false,
                image_format: "image/bmp".into(),
                image_data_base64: base64_str,
                temp_file_path: temp_path,
                error: None,
            })
        }
    }
    #[cfg(not(target_os = "windows"))]
    {
        let count = CAPTURE_COUNTER.fetch_add(1, Ordering::SeqCst);
        let capture_id = format!("region_mock_{}", count);
        Ok(ScreenCaptureResult {
            capture_id,
            captured_at: chrono::Utc::now().to_rfc3339(),
            source: "region".into(),
            window_id: None,
            executable: "mock_region".into(),
            window_title: format!("Region ({}, {}, {}x{})", x, y, width, height),
            bounds: WindowBounds { x, y, width, height },
            scale_factor: 1.0,
            dpi: 96,
            width: width as u32,
            height: height as u32,
            is_secure_desktop: false,
            image_format: "image/bmp".into(),
            image_data_base64: Some("data:image/bmp;base64,Qk0AAAAAAAAAAAAAAA==".into()),
            temp_file_path: None,
            error: None,
        })
    }
}

#[tauri::command]
fn get_active_window_elements() -> Result<UIElementNode, String> {
    #[cfg(target_os = "windows")]
    {
        use windows_sys::Win32::Foundation::*;
        use windows_sys::Win32::UI::WindowsAndMessaging::*;

        unsafe {
            let hwnd = GetForegroundWindow();
            if hwnd.is_null() {
                return Ok(UIElementNode {
                    id: "root".into(),
                    name: "No Active Window".into(),
                    role: "window".into(),
                    class_name: "Desktop".into(),
                    bounds: None,
                    is_enabled: false,
                    is_visible: false,
                    children: Vec::new(),
                });
            }

            let mut title_buf = [0u16; 512];
            let len = GetWindowTextW(hwnd, title_buf.as_mut_ptr(), 512);
            let window_title = if len > 0 {
                String::from_utf16_lossy(&title_buf[..len as usize])
            } else {
                String::new()
            };

            let mut class_buf = [0u16; 256];
            let class_len = GetClassNameW(hwnd, class_buf.as_mut_ptr(), 256);
            let class_name = if class_len > 0 {
                String::from_utf16_lossy(&class_buf[..class_len as usize])
            } else {
                String::new()
            };

            let mut rect = RECT { left: 0, top: 0, right: 0, bottom: 0 };
            GetWindowRect(hwnd, &mut rect);
            let bounds = WindowBounds {
                x: rect.left,
                y: rect.top,
                width: (rect.right - rect.left).max(0),
                height: (rect.bottom - rect.top).max(0),
            };

            // Enumerate child HWNDs (up to 30 elements)
            struct EnumContext {
                children: Vec<UIElementNode>,
            }

            unsafe extern "system" fn enum_child_proc(child_hwnd: HWND, lparam: LPARAM) -> BOOL {
                let ctx = &mut *(lparam as *mut EnumContext);
                if ctx.children.len() >= 30 {
                    return 0; // Stop enumeration
                }

                let mut text_buf = [0u16; 256];
                let text_len = GetWindowTextW(child_hwnd, text_buf.as_mut_ptr(), 256);
                let text = if text_len > 0 {
                    String::from_utf16_lossy(&text_buf[..text_len as usize])
                } else {
                    String::new()
                };

                let mut cls_buf = [0u16; 256];
                let cls_len = GetClassNameW(child_hwnd, cls_buf.as_mut_ptr(), 256);
                let cls = if cls_len > 0 {
                    String::from_utf16_lossy(&cls_buf[..cls_len as usize])
                } else {
                    String::new()
                };

                let is_vis = IsWindowVisible(child_hwnd) != 0;
                let style = GetWindowLongW(child_hwnd, GWL_STYLE);
                let is_en = (style & (WS_DISABLED as i32)) == 0;

                let mut c_rect = RECT { left: 0, top: 0, right: 0, bottom: 0 };
                GetWindowRect(child_hwnd, &mut c_rect);
                let c_bounds = WindowBounds {
                    x: c_rect.left,
                    y: c_rect.top,
                    width: (c_rect.right - c_rect.left).max(0),
                    height: (c_rect.bottom - c_rect.top).max(0),
                };

                let role = if cls.to_lowercase().contains("button") {
                    "button"
                } else if cls.to_lowercase().contains("edit") {
                    "input"
                } else if cls.to_lowercase().contains("combobox") {
                    "combobox"
                } else if cls.to_lowercase().contains("list") {
                    "list"
                } else {
                    "control"
                };

                ctx.children.push(UIElementNode {
                    id: format!("hwnd_{:p}", child_hwnd),
                    name: text,
                    role: role.into(),
                    class_name: cls,
                    bounds: Some(c_bounds),
                    is_enabled: is_en,
                    is_visible: is_vis,
                    children: Vec::new(),
                });

                1 // Continue
            }

            let mut context = EnumContext { children: Vec::new() };
            EnumChildWindows(
                hwnd,
                Some(enum_child_proc),
                &mut context as *mut _ as LPARAM,
            );

            Ok(UIElementNode {
                id: format!("hwnd_{:p}", hwnd),
                name: window_title,
                role: "window".into(),
                class_name,
                bounds: Some(bounds),
                is_enabled: true,
                is_visible: true,
                children: context.children,
            })
        }
    }
    #[cfg(not(target_os = "windows"))]
    {
        Ok(UIElementNode {
            id: "mock_window".into(),
            name: "Mock Window".into(),
            role: "window".into(),
            class_name: "MockWindowClass".into(),
            bounds: Some(WindowBounds { x: 0, y: 0, width: 1280, height: 800 }),
            is_enabled: true,
            is_visible: true,
            children: vec![
                UIElementNode {
                    id: "btn_1".into(),
                    name: "Search".into(),
                    role: "button".into(),
                    class_name: "Button".into(),
                    bounds: Some(WindowBounds { x: 20, y: 20, width: 100, height: 35 }),
                    is_enabled: true,
                    is_visible: true,
                    children: Vec::new(),
                },
                UIElementNode {
                    id: "input_1".into(),
                    name: "Query".into(),
                    role: "input".into(),
                    class_name: "Edit".into(),
                    bounds: Some(WindowBounds { x: 130, y: 20, width: 250, height: 35 }),
                    is_enabled: true,
                    is_visible: true,
                    children: Vec::new(),
                },
            ],
        })
    }
}

#[tauri::command]
fn clear_captures_cache() -> Result<u32, String> {
    let dir = get_temp_captures_dir();
    let mut count = 0u32;
    if let Ok(entries) = fs::read_dir(&dir) {
        for entry in entries.flatten() {
            if entry.path().is_file() {
                if fs::remove_file(entry.path()).is_ok() {
                    count += 1;
                }
            }
        }
    }
    Ok(count)
}

#[tauri::command]
fn get_autostart_status() -> Result<bool, String> {
    #[cfg(target_os = "windows")]
    {
        use winreg::enums::*;
        use winreg::RegKey;

        let hkcu = RegKey::predef(HKEY_CURRENT_USER);
        let run_key = match hkcu.open_subkey("Software\\Microsoft\\Windows\\CurrentVersion\\Run") {
            Ok(k) => k,
            Err(_) => return Ok(false),
        };

        let val: Result<String, _> = run_key.get_value("TARS");
        Ok(val.is_ok())
    }
    #[cfg(not(target_os = "windows"))]
    {
        Ok(false)
    }
}

#[tauri::command]
fn set_autostart(enabled: bool) -> Result<bool, String> {
    #[cfg(target_os = "windows")]
    {
        use winreg::enums::*;
        use winreg::RegKey;

        let hkcu = RegKey::predef(HKEY_CURRENT_USER);
        let (run_key, _) = hkcu
            .create_subkey("Software\\Microsoft\\Windows\\CurrentVersion\\Run")
            .map_err(|e| e.to_string())?;

        if enabled {
            let exe_path = std::env::current_exe().map_err(|e| e.to_string())?;
            let exe_str = exe_path.to_string_lossy().to_string();
            run_key
                .set_value("TARS", &exe_str)
                .map_err(|e| e.to_string())?;
            Ok(true)
        } else {
            let _ = run_key.delete_value("TARS");
            Ok(false)
        }
    }
    #[cfg(not(target_os = "windows"))]
    {
        Ok(enabled)
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let summon_shortcut_space = "Ctrl+Shift+Space".parse::<Shortcut>().unwrap();
    let summon_shortcut_t = "Ctrl+Shift+T".parse::<Shortcut>().unwrap();
    let ptt_shortcut = "Ctrl+Shift+V".parse::<Shortcut>().unwrap();

    let summon_space_setup = summon_shortcut_space.clone();
    let summon_t_setup = summon_shortcut_t.clone();
    let ptt_setup = ptt_shortcut.clone();

    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(move |app, shortcut, event| {
                    if event.state() == ShortcutState::Pressed {
                        if shortcut == &summon_shortcut_space || shortcut == &summon_shortcut_t {
                            let _ = toggle_hud_impl(app, Some("voice"));
                        } else if shortcut == &ptt_shortcut {
                            let _ = app.emit("tars://ptt-toggle", ());
                        }
                    }
                })
                .build(),
        )
        .invoke_handler(tauri::generate_handler![
            greet,
            mark_frontend_ready,
            toggle_compact_mode,
            set_window_size,
            is_always_on_top,
            summon_hud,
            hide_hud,
            toggle_hud,
            exit_app,
            get_active_window_context,
            get_monitors_geometry,
            capture_active_window,
            capture_chart_window,
            capture_screen_region,
            get_active_window_elements,
            clear_captures_cache,
            get_autostart_status,
            set_autostart,
            wake_engine_status,
            set_wake_playback_state,
        ])
        .on_page_load(|webview, payload| {
            if webview.label() == "main"
                && matches!(payload.event(), tauri::webview::PageLoadEvent::Finished)
            {
                let _ = webview.window().set_title("TARS Ready");
            }
        })
        .on_window_event(|window, event| {
            // M2A/M2B Background persistence (Close-to-tray)
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .setup(move |app| {
            // Setup System Tray Menu
            let summon_i = MenuItem::with_id(
                app,
                "summon_hud",
                "Summon TARS HUD (Ctrl+Shift+Space)",
                true,
                None::<&str>,
            )?;
            let show_main_i = MenuItem::with_id(
                app,
                "show_main",
                "Open Main Dashboard",
                true,
                None::<&str>,
            )?;
            let sep1 = PredefinedMenuItem::separator(app)?;
            let ptt_i = MenuItem::with_id(
                app,
                "trigger_ptt",
                "Trigger Voice PTT (Ctrl+Shift+V)",
                true,
                None::<&str>,
            )?;
            let sep2 = PredefinedMenuItem::separator(app)?;
            let quit_i = MenuItem::with_id(app, "quit", "Quit TARS", true, None::<&str>)?;

            let tray_menu = Menu::with_items(
                app,
                &[&summon_i, &show_main_i, &sep1, &ptt_i, &sep2, &quit_i],
            )?;

            let icon = app.default_window_icon().cloned().unwrap();
            let _tray = TrayIconBuilder::new()
                .menu(&tray_menu)
                .show_menu_on_left_click(false)
                .icon(icon)
                .tooltip("TARS Windows Assistant (Running in Background)")
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "summon_hud" => {
                        let _ = summon_hud_impl(app, Some("voice"));
                    }
                    "show_main" => {
                        let _ = show_main_impl(app);
                    }
                    "trigger_ptt" => {
                        let _ = app.emit("tars://ptt-toggle", ());
                    }
                    "quit" => {
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        let _ = toggle_hud_impl(app, Some("voice"));
                    }
                })
                .build(app)?;

            // Register global shortcuts
            #[cfg(desktop)]
            {
                let _ = app.global_shortcut().register(summon_space_setup);
                let _ = app.global_shortcut().register(summon_t_setup);
                let _ = app.global_shortcut().register(ptt_setup);
            }

            // Start listening for "Hey TARS" immediately, on its own
            // background thread -- independent of whether any window is
            // ever shown. The main window itself starts hidden (see
            // tauri.conf.json `visible: false`); this is what lets TARS
            // run as a true background/tray app rather than a dashboard
            // that happens to also listen.
            wake_engine::start(app.handle().clone());

            // Non-intrusive background chart observation (TARS Alexa-Speed
            // Phase C): polls a discovered chart window via
            // Windows.Graphics.Capture -- never hides/focuses TARS's own
            // window, unlike the user-triggered capture_chart_window path.
            #[cfg(target_os = "windows")]
            chart_watcher::start(app.handle().clone());

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
