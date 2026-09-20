//! Non-intrusive per-window capture via Windows.Graphics.Capture (WGC).
//!
//! Built specifically for `BackgroundChartWatcher` (TARS Alexa-Speed Phase
//! C), which needs to poll the chart window every 1-2s without ever
//! disturbing the user. The existing `capture_chart_window` command
//! (lib.rs) hides TARS, sleeps 220ms for DWM to repaint, does a screen-DC
//! BitBlt, then restores and **steals OS input focus back** on every call
//! (`window.set_focus()`) -- fine for a single user-triggered capture, but
//! unusable for a silent background loop: every cycle would flicker TARS
//! and yank keyboard focus away from whatever the user is doing in
//! TradingView.
//!
//! WGC's per-window capture (`GraphicsCaptureItem::CreateForWindow`) reads
//! the target window's own DWM redirection surface directly -- it does not
//! matter what is on top of that window in Z-order, and it does not
//! require hiding or focusing anything. It also works correctly against
//! GPU-composited content (the target here is a real chart window,
//! confirmed by a live capture during Phase A's baseline run to be a
//! native "TradingView" desktop process, not necessarily anything simpler
//! like a plain GDI-rendered window) where the older `PrintWindow`/
//! `PW_RENDERFULLCONTENT` API is well known to be unreliable.
//!
//! This module is capture-only: it does not decide *when* to capture, does
//! not hash/diff frames, and does not talk to the backend. That orchestration
//! is `chart_watcher.rs` (Phase C1/C2). Keeping this module narrowly scoped
//! to "given an hwnd, get a frame" makes it independently testable and
//! keeps the existing, working `capture_chart_window` command completely
//! untouched -- this is new, additive capability, not a replacement.
use std::mem::size_of;

use windows::core::{Interface, HRESULT};
use windows::Foundation::TypedEventHandler;
use windows::Graphics::Capture::{
    Direct3D11CaptureFramePool, GraphicsCaptureItem, GraphicsCaptureSession,
};
use windows::Graphics::DirectX::DirectXPixelFormat;
use windows::Win32::Foundation::HWND;
use windows::Win32::Graphics::Direct3D::D3D_DRIVER_TYPE_HARDWARE;
use windows::Win32::Graphics::Direct3D11::{
    D3D11CreateDevice, ID3D11Device, ID3D11DeviceContext, ID3D11Texture2D,
    D3D11_CPU_ACCESS_READ, D3D11_CREATE_DEVICE_BGRA_SUPPORT, D3D11_MAP_READ, D3D11_SDK_VERSION,
    D3D11_TEXTURE2D_DESC, D3D11_USAGE_STAGING,
};
use windows::Win32::Graphics::Dxgi::IDXGIDevice;
use windows::Win32::System::Com::{CoInitializeEx, COINIT_MULTITHREADED};
use windows::Win32::System::WinRT::Direct3D11::CreateDirect3D11DeviceFromDXGIDevice;
use windows::Win32::System::WinRT::Graphics::Capture::IGraphicsCaptureItemInterop;
use windows::Win32::System::WinRT::{RoInitialize, RO_INIT_MULTITHREADED};

/// Raw captured pixels -- top-down BGRA8, exactly `width * height * 4`
/// bytes. Conversion to PNG (or whatever format a caller needs) happens
/// outside this module, matching capture_chart_window's own separation
/// (raw bytes -> `create_bmp_bytes` there; a caller here would build a PNG
/// via the `image` crate or hand the raw buffer to Python for Pillow to
/// decode -- not decided by this capture-only module).
pub struct CapturedFrame {
    pub width: u32,
    pub height: u32,
    pub bgra: Vec<u8>,
}

/// One WGC capture session bound to a specific target window. Expensive to
/// construct (D3D11 device + frame pool + session setup) and cheap to poll
/// repeatedly (`try_capture_frame`) -- exactly the shape a background
/// watcher polling every 1-2s needs: build once when the target hwnd is
/// discovered, reuse across ticks, tear down only when the hwnd changes or
/// the watcher stops.
pub struct WgcCapture {
    _d3d_device: ID3D11Device,
    d3d_context: ID3D11DeviceContext,
    frame_pool: Direct3D11CaptureFramePool,
    session: GraphicsCaptureSession,
    item: GraphicsCaptureItem,
}

// SAFETY: every COM/WinRT object held here is only ever touched from the
// single background thread that owns this WgcCapture (see chart_watcher.rs)
// -- there is no cross-thread sharing of the interfaces themselves, only of
// the CapturedFrame bytes this struct produces. Rust's auto-trait inference
// can't see that COM objects behind raw pointers are thread-affine-by-
// convention rather than genuinely unsendable, so this is an explicit,
// deliberate assertion of the same single-thread-owner contract
// wake_engine.rs already relies on for its own OS-thread-owned state.
unsafe impl Send for WgcCapture {}

impl WgcCapture {
    /// Initializes COM/WinRT for the calling thread (must be called once
    /// per thread before constructing a WgcCapture -- idempotent to call
    /// again on the same thread) and builds a capture session for `hwnd`.
    pub fn new(hwnd: isize) -> Result<Self, String> {
        unsafe {
            // COINIT_MULTITHREADED because this runs on a dedicated
            // background thread with no UI message pump -- there is
            // nothing here that needs an STA/message-loop-driven apartment.
            let _ = CoInitializeEx(None, COINIT_MULTITHREADED);
            RoInitialize(RO_INIT_MULTITHREADED)
                .map_err(|e| format!("RoInitialize failed: {e}"))?;
        }

        let target = HWND(hwnd as *mut core::ffi::c_void);
        let item = create_capture_item_for_window(target)?;

        let (d3d_device, d3d_context) = create_d3d_device()?;
        let dxgi_device: IDXGIDevice = d3d_device
            .cast()
            .map_err(|e| format!("QueryInterface(IDXGIDevice) failed: {e}"))?;
        let winrt_device = unsafe { CreateDirect3D11DeviceFromDXGIDevice(&dxgi_device) }
            .map_err(|e| format!("CreateDirect3D11DeviceFromDXGIDevice failed: {e}"))?;
        let winrt_device: windows::Graphics::DirectX::Direct3D11::IDirect3DDevice = winrt_device
            .cast()
            .map_err(|e| format!("cast to IDirect3DDevice failed: {e}"))?;

        let size = item.Size().map_err(|e| format!("GraphicsCaptureItem.Size failed: {e}"))?;

        let frame_pool = Direct3D11CaptureFramePool::Create(
            &winrt_device,
            DirectXPixelFormat::B8G8R8A8UIntNormalized,
            1,
            size,
        )
        .map_err(|e| format!("Direct3D11CaptureFramePool::Create failed: {e}"))?;

        let session = frame_pool
            .CreateCaptureSession(&item)
            .map_err(|e| format!("CreateCaptureSession failed: {e}"))?;

        // No visible yellow capture border where the OS supports
        // suppressing it (Windows 11 22H2+) -- best-effort: an older OS
        // build without this API surface still captures correctly, it
        // just shows the standard capture border. Never treated as fatal.
        let _ = session.SetIsBorderRequired(false);

        session
            .StartCapture()
            .map_err(|e| format!("StartCapture failed: {e}"))?;

        Ok(Self { _d3d_device: d3d_device, d3d_context, frame_pool, session, item })
    }

    /// Non-blocking: returns the newest available frame, or `None` if
    /// nothing new has arrived since the last call (mirrors
    /// `TryGetNextFrame`'s own semantics -- this is intentionally a poll,
    /// not a blocking wait, so a caller on a 1-2s tick never stalls
    /// waiting for a frame that may not have changed).
    pub fn try_capture_frame(&self) -> Result<Option<CapturedFrame>, String> {
        // Confirmed against a real live capture session: when no new frame
        // has arrived yet (e.g. immediately after StartCapture, before DWM
        // has produced one), TryGetNextFrame's WinRT binding does not
        // return an empty Option -- it returns Err with HRESULT S_OK
        // wrapping a null interface pointer. That is this API's documented
        // "no frame yet" signal, not a real failure; only a non-S_OK
        // HRESULT is an actual error.
        let frame = match self.frame_pool.TryGetNextFrame() {
            Ok(frame) => frame,
            Err(e) if e.code().is_ok() => return Ok(None),
            Err(e) => return Err(format!("TryGetNextFrame failed: {e}")),
        };

        let surface = frame
            .Surface()
            .map_err(|e| format!("Frame.Surface failed: {e}"))?;
        let access: windows::Win32::System::WinRT::Direct3D11::IDirect3DDxgiInterfaceAccess =
            surface
                .cast()
                .map_err(|e| format!("cast to IDirect3DDxgiInterfaceAccess failed: {e}"))?;
        let texture: ID3D11Texture2D = unsafe { access.GetInterface() }
            .map_err(|e| format!("GetInterface(ID3D11Texture2D) failed: {e}"))?;

        let mut desc = D3D11_TEXTURE2D_DESC::default();
        unsafe { texture.GetDesc(&mut desc) };

        let staging_desc = D3D11_TEXTURE2D_DESC {
            Usage: D3D11_USAGE_STAGING,
            BindFlags: 0,
            CPUAccessFlags: D3D11_CPU_ACCESS_READ.0 as u32,
            MiscFlags: 0,
            ..desc
        };
        let mut staging: Option<ID3D11Texture2D> = None;
        unsafe { self.staging_device().CreateTexture2D(&staging_desc, None, Some(&mut staging)) }
            .map_err(|e| format!("CreateTexture2D(staging) failed: {e}"))?;
        let staging = staging.ok_or_else(|| "CreateTexture2D returned no texture".to_string())?;

        unsafe { self.d3d_context.CopyResource(&staging, &texture) };

        let mapped = unsafe {
            let mut mapped = Default::default();
            self.d3d_context
                .Map(&staging, 0, D3D11_MAP_READ, 0, Some(&mut mapped))
                .map_err(|e| format!("Map(staging) failed: {e}"))?;
            mapped
        };

        let width = desc.Width;
        let height = desc.Height;
        let row_pitch = mapped.RowPitch as usize;
        let bytes_per_pixel = size_of::<u32>();
        let mut bgra = vec![0u8; (width as usize) * (height as usize) * bytes_per_pixel];

        unsafe {
            let src = mapped.pData as *const u8;
            for row in 0..height as usize {
                let src_row = src.add(row * row_pitch);
                let dst_start = row * (width as usize) * bytes_per_pixel;
                let dst_row = bgra.as_mut_ptr().add(dst_start);
                std::ptr::copy_nonoverlapping(src_row, dst_row, (width as usize) * bytes_per_pixel);
            }
            self.d3d_context.Unmap(&staging, 0);
        }

        Ok(Some(CapturedFrame { width, height, bgra }))
    }

    /// True once the target window this session was built for no longer
    /// exists -- the caller (chart_watcher.rs) should tear this session
    /// down and rediscover the target rather than continuing to poll a
    /// dead session (per Part 26's "if TradingView closes/reopens... the
    /// watcher pauses safely / resumes" requirement).
    pub fn is_target_closed(&self) -> bool {
        self.item.Size().is_err()
    }

    fn staging_device(&self) -> &ID3D11Device {
        &self._d3d_device
    }
}

impl Drop for WgcCapture {
    fn drop(&mut self) {
        let _ = self.session.Close();
        let _ = self.frame_pool.Close();
    }
}

fn create_capture_item_for_window(hwnd: HWND) -> Result<GraphicsCaptureItem, String> {
    let interop: IGraphicsCaptureItemInterop =
        windows::core::factory::<GraphicsCaptureItem, IGraphicsCaptureItemInterop>()
            .map_err(|e: windows::core::Error| format!("get IGraphicsCaptureItemInterop factory failed: {e}"))?;
    unsafe { interop.CreateForWindow(hwnd) }
        .map_err(|e| format!("CreateForWindow failed (window may not support capture): {e}"))
}

fn create_d3d_device() -> Result<(ID3D11Device, ID3D11DeviceContext), String> {
    let mut device: Option<ID3D11Device> = None;
    let mut context: Option<ID3D11DeviceContext> = None;
    let result: Result<(), HRESULT> = unsafe {
        D3D11CreateDevice(
            None,
            D3D_DRIVER_TYPE_HARDWARE,
            windows::Win32::Foundation::HMODULE::default(),
            D3D11_CREATE_DEVICE_BGRA_SUPPORT,
            None,
            D3D11_SDK_VERSION,
            Some(&mut device),
            None,
            Some(&mut context),
        )
        .map_err(|e| e.code())
    };
    result.map_err(|code| format!("D3D11CreateDevice failed: {code:?}"))?;

    let device = device.ok_or_else(|| "D3D11CreateDevice returned no device".to_string())?;
    let context = context.ok_or_else(|| "D3D11CreateDevice returned no context".to_string())?;
    Ok((device, context))
}

/// Bound-safety helper unused directly (kept for the TypedEventHandler
/// import to remain meaningful documentation of the event-based
/// alternative this module deliberately does not use -- see module docs:
/// polling via TryGetNextFrame avoids needing a DispatcherQueue message
/// pump on this background thread).
#[allow(dead_code)]
fn _unused_typed_event_handler_reference() -> Option<TypedEventHandler<GraphicsCaptureSession, windows::core::IInspectable>> {
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Real hardware/live-window integration test -- not run by default
    /// (`cargo test` skips `#[ignore]`d tests), since it needs an actual
    /// TradingView window open on the machine running it. Run explicitly
    /// with `cargo test --lib capture_wgc -- --ignored --nocapture` to
    /// verify the whole WGC pipeline (window discovery -> D3D11 device ->
    /// capture session -> real frame -> non-degenerate pixel data)
    /// against a live target, the one thing pure unit tests of
    /// average_hash/base64/etc. cannot prove: that the actual COM/WinRT
    /// interop is correct.
    #[test]
    #[ignore]
    fn captures_a_real_frame_from_a_live_tradingview_window() {
        let hwnd = find_tradingview_window_for_test().expect(
            "no visible window with 'tradingview' in its title/process was found -- open TradingView before running this test",
        );

        let capture = WgcCapture::new(hwnd).expect("WgcCapture::new failed against a real window");
        let frame = capture
            .capture_frame_blocking_for_test(20)
            .expect("no frame arrived from the live capture session");

        assert!(frame.width > 0 && frame.height > 0, "captured frame must have nonzero dimensions");
        assert_eq!(
            frame.bgra.len(),
            (frame.width as usize) * (frame.height as usize) * 4,
            "buffer size must match width*height*4 for BGRA8"
        );

        // A real chart is not a single solid color -- if every byte were
        // identical, that would indicate a black/blank capture (the
        // classic PrintWindow-on-GPU-composited-content failure mode this
        // module exists specifically to avoid), not a genuine frame.
        let first = frame.bgra[0];
        let all_identical = frame.bgra.iter().all(|&b| b == first);
        assert!(!all_identical, "captured frame is a single solid color -- capture likely failed silently");

        eprintln!(
            "[capture_wgc test] captured {}x{} real frame, {} bytes, first_byte={}, all_identical={}",
            frame.width,
            frame.height,
            frame.bgra.len(),
            first,
            all_identical
        );

        // Manual visual verification aid only -- write a real, viewable
        // BMP to a fixed scratch path so a human (or this session) can
        // open it and confirm the image is actually the chart, right side
        // up, correct colors -- not just "not a single solid color."
        if let Ok(dir) = std::env::var("TARS_WGC_TEST_OUT_DIR") {
            let bmp = test_encode_bmp_bottom_up(&frame);
            let path = std::path::Path::new(&dir).join("wgc_live_capture.bmp");
            std::fs::write(&path, &bmp).expect("failed to write verification BMP");
            eprintln!("[capture_wgc test] wrote verification image to {}", path.display());
        }
    }

    fn test_encode_bmp_bottom_up(frame: &CapturedFrame) -> Vec<u8> {
        let (w, h) = (frame.width as usize, frame.height as usize);
        let row_bytes = w * 4;
        let mut bottom_up = vec![0u8; frame.bgra.len()];
        for y in 0..h {
            let src_start = y * row_bytes;
            let dst_start = (h - 1 - y) * row_bytes;
            bottom_up[dst_start..dst_start + row_bytes].copy_from_slice(&frame.bgra[src_start..src_start + row_bytes]);
        }
        crate::create_bmp_bytes(frame.width, frame.height, 32, &bottom_up)
    }

    /// Matches by process name OR window title -- the real "TradingView"
    /// desktop app (confirmed live during Phase A's baseline run) does NOT
    /// put "TradingView" in its window title (it shows the symbol/price
    /// instead, e.g. "XAUUSD ... / Practice"); only its process name is
    /// "TradingView". A browser-hosted chart would be the opposite case
    /// (generic browser process name, "... - TradingView" in the title).
    /// Checking only one of the two would miss the actual live target --
    /// exactly what an earlier, title-only version of this test helper did
    /// before this was caught by running it against the real window.
    fn find_tradingview_window_for_test() -> Option<isize> {
        use windows_sys::Win32::Foundation::*;
        use windows_sys::Win32::System::Threading::*;
        use windows_sys::Win32::UI::WindowsAndMessaging::*;

        unsafe extern "system" fn enum_proc(hwnd: HWND, lparam: LPARAM) -> BOOL {
            let out = &mut *(lparam as *mut Option<isize>);
            if out.is_some() || IsWindowVisible(hwnd) == 0 {
                return 1;
            }
            let len = GetWindowTextLengthW(hwnd);
            if len == 0 {
                return 1;
            }
            let mut buf = vec![0u16; (len + 1) as usize];
            let n = GetWindowTextW(hwnd, buf.as_mut_ptr(), buf.len() as i32);
            let title = String::from_utf16_lossy(&buf[..n as usize]);

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
                *out = Some(hwnd as isize);
            }
            1
        }

        let mut found: Option<isize> = None;
        unsafe {
            EnumWindows(Some(enum_proc), &mut found as *mut _ as LPARAM);
        }
        found
    }

    impl WgcCapture {
        fn capture_frame_blocking_for_test(&self, max_attempts: u32) -> Result<CapturedFrame, String> {
            for attempt in 0..max_attempts {
                if let Some(frame) = self.try_capture_frame()? {
                    return Ok(frame);
                }
                std::thread::sleep(std::time::Duration::from_millis(50 * (attempt as u64 + 1)));
            }
            Err("no frame became available within the retry budget".to_string())
        }
    }
}
