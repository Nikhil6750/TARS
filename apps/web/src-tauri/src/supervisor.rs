//! BackendSupervisor -- the Tauri desktop shell supervises the Python
//! backend as a child process (mission: FINAL INFRASTRUCTURE MISSION,
//! "Tauri companion is the durable desktop parent/supervisor -> Python
//! backend child/service", sections 2/3).
//!
//! Adopt-or-spawn (mission section 4, single instance): on start, if a
//! backend is ALREADY healthy on the configured port (e.g. started by
//! scripts/start_tars.ps1, or a leftover from a previous run this instance
//! didn't launch), this supervisor adopts it -- tracked via health polling
//! only, no owned Child handle -- rather than spawning a duplicate. If
//! nothing is listening, it spawns `python run.py` itself and owns the
//! Child handle so it can detect an unexpected exit and apply bounded
//! backoff before restarting.
//!
//! A clean stop (Quit, or Restart TARS) is always COOPERATIVE first: this
//! module POSTs /api/v1/runtime/shutdown (uvicorn's own graceful
//! should_exit path -- runs the Python lifespan's cleanup) and only
//! force-kills the child if it doesn't exit within a bound. `intentional`
//! is set BEFORE that happens, so the supervising loop's own
//! crash-detection never mistakes a commanded stop for a crash and never
//! auto-restarts after it (mission section 3/8).
//!
//! Runs on its own background OS thread (same independence-from-the-panel
//! pattern as `wake_engine.rs`/`chart_watcher.rs`), not async/tokio --
//! ureq's HTTP calls and `Child::wait()` are both blocking.
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter};

#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

const HEALTH_URL: &str = "http://127.0.0.1:8000/api/v1/health";
const SHUTDOWN_URL: &str = "http://127.0.0.1:8000/api/v1/runtime/shutdown";
const REBASE_URL: &str = "http://127.0.0.1:8000/api/v1/runtime/resume_rebase";
const POLL_INTERVAL: Duration = Duration::from_secs(5);
const GRACEFUL_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(10);
const READY_WAIT_TIMEOUT: Duration = Duration::from_secs(30);
// A poll-loop gap much larger than POLL_INTERVAL means the OS itself was
// suspended (sleep/hibernate) for that stretch, not that this thread's own
// scheduling was merely slow -- a live, non-suspended Windows session does
// not stall a plain `thread::sleep` by multiples of its requested duration.
const SLEEP_GAP_THRESHOLD: Duration = Duration::from_secs(60);

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum BackendHealth {
    Starting,
    Ready,
    Restarting,
    Degraded,
}

/// Pure backoff policy (mission section 3's example policy), separated
/// from any process I/O so it is directly unit-testable: failure 1 -> short
/// delay, failure 2 -> longer, failure 3 -> longer bounded delay, then
/// stays capped -- never an unbounded ramp, never a tight restart-spawn
/// loop (mission section 3/21C).
pub fn backoff_delay(consecutive_failures: u32) -> Duration {
    match consecutive_failures {
        0 => Duration::from_secs(0),
        1 => Duration::from_secs(2),
        2 => Duration::from_secs(5),
        3 => Duration::from_secs(15),
        _ => Duration::from_secs(30),
    }
}

/// After this many consecutive crash-restarts without ever reaching Ready
/// again, stop retrying automatically and surface Degraded instead of
/// spinning forever (mission: "remain degraded and expose an error instead
/// of spinning endlessly").
pub const MAX_CONSECUTIVE_FAILURES: u32 = 6;

/// Pure decision: given the current consecutive-failure count, should the
/// supervisor attempt another restart, or give up and report Degraded?
/// Unit-tested directly against the constant above.
pub fn should_attempt_restart(consecutive_failures: u32) -> bool {
    consecutive_failures < MAX_CONSECUTIVE_FAILURES
}

pub struct BackendSupervisor {
    child: Mutex<Option<Child>>,
    intentional_stop: AtomicBool,
    consecutive_failures: AtomicU32,
    health: Mutex<BackendHealth>,
    backend_dir: PathBuf,
    adopted: AtomicBool, // true if we never spawned the current backend ourselves
}

impl BackendSupervisor {
    pub fn new(backend_dir: PathBuf) -> Self {
        Self {
            child: Mutex::new(None),
            intentional_stop: AtomicBool::new(false),
            consecutive_failures: AtomicU32::new(0),
            health: Mutex::new(BackendHealth::Starting),
            backend_dir,
            adopted: AtomicBool::new(false),
        }
    }

    pub fn health(&self) -> BackendHealth {
        *self.health.lock().unwrap()
    }

    fn set_health(&self, app: &AppHandle, state: BackendHealth) {
        let mut guard = self.health.lock().unwrap();
        if *guard != state {
            *guard = state;
            let _ = app.emit("tars://backend-health", state);
        }
    }

    /// Starts the background supervising thread. Call once, from `.setup()`.
    pub fn start(self: &Arc<Self>, app: AppHandle) {
        let this = Arc::clone(self);
        std::thread::spawn(move || this.run(app));
    }

    fn run(&self, app: AppHandle) {
        if health_check_once() {
            // Already healthy -- adopt rather than spawn a duplicate
            // (mission section 4: single instance).
            self.adopted.store(true, Ordering::SeqCst);
            self.set_health(&app, BackendHealth::Ready);
        } else {
            self.spawn_and_wait_ready(&app);
        }

        let mut last_tick = Instant::now();
        loop {
            std::thread::sleep(POLL_INTERVAL);
            let elapsed = last_tick.elapsed();
            last_tick = Instant::now();
            if elapsed > SLEEP_GAP_THRESHOLD {
                eprintln!(
                    "[TARS][supervisor] poll gap of {:?} detected -- system was likely suspended; rebasing",
                    elapsed
                );
                let _ = app.emit("tars://system-resumed", ());
                request_resume_rebase();
            }

            if self.intentional_stop.load(Ordering::SeqCst) {
                continue; // a cooperative stop is in flight; the caller owns the next transition
            }

            let child_exited = {
                let mut guard = self.child.lock().unwrap();
                match guard.as_mut() {
                    Some(child) => matches!(child.try_wait(), Ok(Some(_))),
                    None => false,
                }
            };
            let healthy = health_check_once();

            if !healthy || child_exited {
                if self.intentional_stop.swap(false, Ordering::SeqCst) {
                    // A stop was requested between the checks above and here.
                    continue;
                }
                self.handle_unexpected_down(&app);
            } else {
                self.consecutive_failures.store(0, Ordering::SeqCst);
                self.set_health(&app, BackendHealth::Ready);
            }
        }
    }

    fn handle_unexpected_down(&self, app: &AppHandle) {
        let failures = self.consecutive_failures.fetch_add(1, Ordering::SeqCst) + 1;
        if !should_attempt_restart(failures) {
            eprintln!(
                "[TARS][supervisor] backend failed {failures} consecutive times; giving up auto-restart, reporting DEGRADED"
            );
            self.set_health(app, BackendHealth::Degraded);
            return;
        }
        self.set_health(app, BackendHealth::Restarting);
        let delay = backoff_delay(failures);
        eprintln!("[TARS][supervisor] backend down (failure {failures}); restarting in {delay:?}");
        std::thread::sleep(delay);
        self.adopted.store(false, Ordering::SeqCst);
        self.spawn_and_wait_ready(app);
    }

    fn spawn_and_wait_ready(&self, app: &AppHandle) {
        self.set_health(app, BackendHealth::Starting);
        match self.spawn_child() {
            Ok(child) => {
                *self.child.lock().unwrap() = Some(child);
            }
            Err(e) => {
                eprintln!("[TARS][supervisor] failed to spawn backend: {e}");
                self.set_health(app, BackendHealth::Degraded);
                return;
            }
        }
        let deadline = Instant::now() + READY_WAIT_TIMEOUT;
        while Instant::now() < deadline {
            if health_check_once() {
                self.set_health(app, BackendHealth::Ready);
                self.consecutive_failures.store(0, Ordering::SeqCst);
                return;
            }
            std::thread::sleep(Duration::from_millis(500));
        }
        eprintln!("[TARS][supervisor] backend did not become healthy within {READY_WAIT_TIMEOUT:?}");
    }

    fn spawn_child(&self) -> std::io::Result<Child> {
        let mut cmd = Command::new("python");
        cmd.arg("run.py").current_dir(&self.backend_dir);
        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        cmd.spawn()
    }

    /// Cooperative stop: POST the graceful-shutdown endpoint, wait a bound
    /// for the process to actually exit, force-kill only as a last resort.
    /// Sets `intentional_stop` FIRST so the supervising loop never treats
    /// this exit as a crash.
    pub fn stop(&self, app: &AppHandle) {
        self.intentional_stop.store(true, Ordering::SeqCst);
        let _ = request_graceful_shutdown();
        let deadline = Instant::now() + GRACEFUL_SHUTDOWN_TIMEOUT;
        while Instant::now() < deadline {
            if !health_check_once() {
                break;
            }
            std::thread::sleep(Duration::from_millis(300));
        }
        let mut guard = self.child.lock().unwrap();
        if let Some(mut child) = guard.take() {
            match child.try_wait() {
                Ok(Some(_)) => {} // already exited gracefully
                _ => {
                    eprintln!("[TARS][supervisor] graceful shutdown timed out; force-killing backend child");
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
        }
        self.adopted.store(false, Ordering::SeqCst);
        self.set_health(app, BackendHealth::Degraded);
        self.intentional_stop.store(false, Ordering::SeqCst);
    }

    /// Deterministic restart: stop (cooperative, bounded) then spawn fresh.
    /// Never loses persisted state (watchlist/daily-brief date live in the
    /// backend's own sqlite db, untouched by this process-level restart).
    pub fn restart(self: &Arc<Self>, app: &AppHandle) {
        self.stop(app);
        self.spawn_and_wait_ready(app);
    }
}

fn health_check_once() -> bool {
    ureq::get(HEALTH_URL)
        .timeout(Duration::from_secs(2))
        .call()
        .map(|r| r.status() == 200)
        .unwrap_or(false)
}

fn request_graceful_shutdown() -> bool {
    ureq::post(SHUTDOWN_URL)
        .timeout(Duration::from_secs(3))
        .send_bytes(b"{}")
        .is_ok()
}

fn request_resume_rebase() -> bool {
    ureq::post(REBASE_URL)
        .timeout(Duration::from_secs(5))
        .send_bytes(b"{}")
        .is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_delay_increases_then_caps() {
        assert_eq!(backoff_delay(0), Duration::from_secs(0));
        assert_eq!(backoff_delay(1), Duration::from_secs(2));
        assert_eq!(backoff_delay(2), Duration::from_secs(5));
        assert_eq!(backoff_delay(3), Duration::from_secs(15));
        // Capped, not an unbounded ramp -- failure 10 is no worse than 4.
        assert_eq!(backoff_delay(4), backoff_delay(10));
        assert_eq!(backoff_delay(4), Duration::from_secs(30));
    }

    #[test]
    fn backoff_delay_is_monotonically_non_decreasing() {
        let mut prev = Duration::from_secs(0);
        for failures in 0..20 {
            let delay = backoff_delay(failures);
            assert!(delay >= prev, "backoff must never shrink as failures accumulate");
            prev = delay;
        }
    }

    #[test]
    fn should_attempt_restart_stops_after_the_bound() {
        assert!(should_attempt_restart(0));
        assert!(should_attempt_restart(MAX_CONSECUTIVE_FAILURES - 1));
        assert!(!should_attempt_restart(MAX_CONSECUTIVE_FAILURES));
        assert!(!should_attempt_restart(MAX_CONSECUTIVE_FAILURES + 100));
    }

    #[test]
    fn a_rapid_crash_loop_cannot_spin_without_bound() {
        // Simulates mission acceptance test C: repeated immediate crashes
        // never produce an unbounded restart attempt count or a zero-delay
        // tight loop.
        let mut total_delay = Duration::from_secs(0);
        let mut attempts = 0u32;
        for failures in 1..=(MAX_CONSECUTIVE_FAILURES + 5) {
            if !should_attempt_restart(failures - 1) {
                break;
            }
            total_delay += backoff_delay(failures);
            attempts += 1;
        }
        assert!(attempts <= MAX_CONSECUTIVE_FAILURES);
        assert!(total_delay > Duration::from_secs(0), "must never be a zero-delay spin loop");
    }
}
