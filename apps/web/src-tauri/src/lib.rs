use tauri::Manager;
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};

#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! You've been greeted from Rust/TARS native backend!", name)
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
            window.set_size(tauri::LogicalSize::new(420.0, 720.0)).map_err(|e| e.to_string())?;
            window.set_always_on_top(true).map_err(|e| e.to_string())?;
        } else {
            window.set_size(tauri::LogicalSize::new(1280.0, 840.0)).map_err(|e| e.to_string())?;
            window.set_always_on_top(false).map_err(|e| e.to_string())?;
        }
        return Ok(next_compact);
    }
    Ok(false)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let toggle_shortcut = "Ctrl+Shift+T".parse::<Shortcut>().unwrap();
    let toggle_shortcut_for_setup = toggle_shortcut.clone();

    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(move |app, shortcut, event| {
                    if shortcut == &toggle_shortcut && event.state() == ShortcutState::Pressed {
                        let _ = toggle_compact_mode(app.clone(), None);
                    }
                })
                .build(),
        )
        .invoke_handler(tauri::generate_handler![greet, toggle_compact_mode, is_always_on_top])
        .setup(move |app| {
            #[cfg(desktop)]
            {
                let _ = app.global_shortcut().register(toggle_shortcut_for_setup);
            }
            #[cfg(debug_assertions)]
            {
                let _window = app.get_webview_window("main").unwrap();
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
