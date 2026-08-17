use serde::{Deserialize, Serialize};
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Emitter, Manager};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};

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

#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! TARS Windows Native Assistant Ready.", name)
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
            window.set_size(tauri::LogicalSize::new(440.0, 740.0)).map_err(|e| e.to_string())?;
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
fn summon_hud(app: tauri::AppHandle, _mode: Option<String>) -> Result<(), String> {
    summon_hud_impl(&app)
}

#[tauri::command]
fn hide_hud(app: tauri::AppHandle) -> Result<(), String> {
    hide_hud_impl(&app)
}

#[tauri::command]
fn toggle_hud(app: tauri::AppHandle, _mode: Option<String>) -> Result<bool, String> {
    toggle_hud_impl(&app)
}

#[tauri::command]
fn exit_app(app: tauri::AppHandle) -> Result<(), String> {
    app.exit(0);
    Ok(())
}

fn summon_hud_impl(app: &tauri::AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("main") {
        window.show().map_err(|e| e.to_string())?;
        window.unminimize().map_err(|e| e.to_string())?;
        window.set_size(tauri::LogicalSize::new(440.0, 740.0)).map_err(|e| e.to_string())?;
        window.set_always_on_top(true).map_err(|e| e.to_string())?;
        window.set_focus().map_err(|e| e.to_string())?;
        let _ = app.emit("tars://summon-hud", ());
        return Ok(());
    }
    Err("Main window not found".into())
}

fn show_main_impl(app: &tauri::AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("main") {
        window.show().map_err(|e| e.to_string())?;
        window.unminimize().map_err(|e| e.to_string())?;
        window.set_size(tauri::LogicalSize::new(1280.0, 840.0)).map_err(|e| e.to_string())?;
        window.set_always_on_top(false).map_err(|e| e.to_string())?;
        window.set_focus().map_err(|e| e.to_string())?;
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

fn toggle_hud_impl(app: &tauri::AppHandle) -> Result<bool, String> {
    if let Some(window) = app.get_webview_window("main") {
        let is_visible = window.is_visible().unwrap_or(false);
        if is_visible {
            let _ = hide_hud_impl(app);
            Ok(false)
        } else {
            let _ = summon_hud_impl(app);
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
            let hwnd = GetForegroundWindow();
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
                            let _ = toggle_hud_impl(app);
                        } else if shortcut == &ptt_shortcut {
                            let _ = app.emit("tars://ptt-toggle", ());
                        }
                    }
                })
                .build(),
        )
        .invoke_handler(tauri::generate_handler![
            greet,
            toggle_compact_mode,
            is_always_on_top,
            summon_hud,
            hide_hud,
            toggle_hud,
            exit_app,
            get_active_window_context,
            get_autostart_status,
            set_autostart,
        ])
        .on_window_event(|window, event| {
            // M2A Criterion 1: Background persistence (Close-to-tray)
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .setup(move |app| {
            // Setup System Tray Menu (M2A Criterion 2)
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
                        let _ = summon_hud_impl(app);
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
                        let _ = toggle_hud_impl(app);
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

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
