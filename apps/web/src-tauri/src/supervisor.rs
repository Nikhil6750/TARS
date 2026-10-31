//! BackendSupervisor -- the Tauri desktop shell supervises the Python
//! backend as a child process (mission: FINAL INFRASTRUCTURE MISSION,
//! "Tauri companion is the durable desktop parent/supervisor -> Python
//! backend child/service", sections 2/3).
//!
//! Two questions are kept strictly apart here, because answering a 200 from
//! the health port with "that must be ours" is how a shell ends up driving,
//! or shutting down, a process that belongs to somebody else:
//!
//! * **May I talk to it?** (compatibility) The process declares itself a
//!   TARS backend and speaks the API contract this release expects. See
//!   `is_compatible` and `app/identity.py`.
//! * **May I stop it?** (ownership) Before spawning, this shell generates an
//!   owner id and a secret and passes them in the child's environment. A
//!   backend reporting that owner id is provably ours; the secret authorises
//!   its shutdown endpoint. Nothing else is ever stopped, however healthy or
//!   compatible it looks.
//!
//! So on the port we may find (`decide`):
//!
//! | Found                         | Action                                  |
//! |-------------------------------|-----------------------------------------|
//! | nothing                       | spawn and own                           |
//! | compatible, ours              | own (reclaims our orphan after a crash) |
//! | compatible, not ours          | attach: use it, never stop it           |
//! | incompatible, ours            | replace: an upgrade left our old build  |
//! | incompatible or not TARS      | conflict: report it, touch nothing      |
//!
//! A conflict deliberately leaves the app degraded. Taking a stranger's
//! process down to free a port would be worse than failing to start.
//!
//! The owner record lives on disk so that a backend orphaned by a shell crash
//! is recognised by the next launch rather than becoming an unstoppable
//! stranger.
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
use crate::backend_endpoint;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter};

#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

const HEALTH_PATH: &str = "/api/v1/health";
const SHUTDOWN_PATH: &str = "/api/v1/runtime/shutdown";
const REBASE_PATH: &str = "/api/v1/runtime/resume_rebase";
const POLL_INTERVAL: Duration = Duration::from_secs(5);
const GRACEFUL_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(10);
// A cold installed backend imports a large dependency set from disk, so this
// bound has to cover that rather than a warm development start.
const READY_WAIT_TIMEOUT: Duration = Duration::from_secs(120);
// A poll-loop gap much larger than POLL_INTERVAL means the OS itself was
// suspended (sleep/hibernate) for that stretch, not that this thread's own
// scheduling was merely slow -- a live, non-suspended Windows session does
// not stall a plain `thread::sleep` by multiples of its requested duration.
const SLEEP_GAP_THRESHOLD: Duration = Duration::from_secs(60);
// How many consecutive polls our own live child may fail before it is treated
// as hung. One slow answer is not a crash, and restarting on it is what turned
// a single stall into a respawn loop.
const MAX_MISSED_POLLS: u32 = 3;

/// What this release expects a backend to declare. Must match
/// `SERVICE_NAME` / `CONTRACT_VERSION` in `apps/backend/app/identity.py`.
pub const EXPECTED_SERVICE: &str = "tars-backend";
pub const EXPECTED_CONTRACT: i64 = 1;

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum BackendHealth {
    Starting,
    Ready,
    Restarting,
    Degraded,
}

/// Our relationship to whatever holds the backend port.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Ownership {
    /// Nothing is running that we know of.
    None,
    /// We started it and may stop it.
    Owned,
    /// Compatible, started by somebody else. We use it and never stop it.
    Attached,
    /// Something we must not use holds the port.
    Conflict,
}

/// The state surfaced to the UI, including why it is what it is.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct BackendStatus {
    pub health: BackendHealth,
    pub ownership: Ownership,
    pub detail: String,
    pub base_url: String,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Deserialize)]
pub struct BackendIdentity {
    pub service: String,
    pub contract_version: i64,
    #[serde(default)]
    pub instance_id: String,
    #[serde(default)]
    pub owner_id: String,
    #[serde(default)]
    pub build_id: String,
}

/// What a health probe found on the port.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Probe {
    /// Nothing is listening.
    Absent,
    /// Something accepted the connection but did not answer in time.
    Unresponsive,
    /// Something answered, and it is not a TARS backend.
    Foreign(String),
    Tars(BackendIdentity),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Decision {
    Spawn,
    Own,
    Attach,
    Replace,
    Conflict(String),
    /// Too little is known to act; look again on the next poll.
    Wait,
}

/// The adoption rule. Must agree with `is_compatible` in
/// `apps/backend/app/identity.py`.
pub fn is_compatible(identity: &BackendIdentity) -> bool {
    identity.service == EXPECTED_SERVICE && identity.contract_version == EXPECTED_CONTRACT
}

/// Pure: what a health response body tells us. A 200 without a TARS identity
/// is `Foreign`, however plausible it looks.
pub fn classify_health_body(body: &str) -> Probe {
    let Ok(value) = serde_json::from_str::<serde_json::Value>(body) else {
        return Probe::Foreign("answers the health path, but not with JSON".to_string());
    };
    let Some(block) = value.get("identity").filter(|b| b.is_object()) else {
        return Probe::Foreign("answers the health path without declaring an identity".to_string());
    };
    match serde_json::from_value::<BackendIdentity>(block.clone()) {
        Ok(identity) if identity.service == EXPECTED_SERVICE => Probe::Tars(identity),
        Ok(identity) => Probe::Foreign(format!("declares itself as {:?}", identity.service)),
        Err(_) => Probe::Foreign("declares an identity this release cannot read".to_string()),
    }
}

/// Pure: the whole ownership policy, so every branch is unit-testable.
pub fn decide(probe: &Probe, our_owner_id: Option<&str>) -> Decision {
    match probe {
        Probe::Absent => Decision::Spawn,
        Probe::Unresponsive => Decision::Wait,
        Probe::Foreign(detail) => Decision::Conflict(format!("the port is held by another program: it {detail}")),
        Probe::Tars(identity) => {
            let ours = our_owner_id.is_some_and(|id| !id.is_empty() && id == identity.owner_id);
            match (is_compatible(identity), ours) {
                (true, true) => Decision::Own,
                (true, false) => Decision::Attach,
                (false, true) => Decision::Replace,
                (false, false) => Decision::Conflict(format!(
                    "the port is held by a TARS backend speaking contract {} (this release needs {}), and this shell did not start it",
                    identity.contract_version, EXPECTED_CONTRACT
                )),
            }
        }
    }
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

/// How to start the backend: the development checkout's `python run.py`, or
/// the runtime bundled with an installed build.
#[derive(Debug, Clone, Default)]
pub struct BackendLaunch {
    pub program: PathBuf,
    pub working_dir: PathBuf,
    pub envs: Vec<(String, String)>,
    pub log_file: Option<PathBuf>,
}

/// The proof of ownership, kept on disk so that it survives a shell crash.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct OwnerRecord {
    pub owner_id: String,
    pub secret: String,
}

impl OwnerRecord {
    fn generate() -> Self {
        Self { owner_id: uuid::Uuid::new_v4().simple().to_string(), secret: uuid::Uuid::new_v4().simple().to_string() }
    }
}

fn read_owner_record(path: &Path) -> Option<OwnerRecord> {
    serde_json::from_str(&std::fs::read_to_string(path).ok()?).ok()
}

pub struct BackendSupervisor {
    child: Mutex<Option<Child>>,
    intentional_stop: AtomicBool,
    consecutive_failures: AtomicU32,
    missed_polls: AtomicU32,
    health: Mutex<BackendHealth>,
    ownership: Mutex<Ownership>,
    detail: Mutex<String>,
    launch: BackendLaunch,
    owner_record_path: Option<PathBuf>,
    owner: Mutex<Option<OwnerRecord>>,
}

impl BackendSupervisor {
    pub fn new(launch: BackendLaunch, owner_record_path: Option<PathBuf>) -> Self {
        let owner = owner_record_path.as_deref().and_then(read_owner_record);
        Self {
            child: Mutex::new(None),
            intentional_stop: AtomicBool::new(false),
            consecutive_failures: AtomicU32::new(0),
            missed_polls: AtomicU32::new(0),
            health: Mutex::new(BackendHealth::Starting),
            ownership: Mutex::new(Ownership::None),
            detail: Mutex::new("starting".to_string()),
            launch,
            owner_record_path,
            owner: Mutex::new(owner),
        }
    }

    pub fn health(&self) -> BackendHealth {
        *self.health.lock().unwrap()
    }

    pub fn status(&self) -> BackendStatus {
        BackendStatus {
            health: self.health(),
            ownership: *self.ownership.lock().unwrap(),
            detail: self.detail.lock().unwrap().clone(),
            base_url: backend_endpoint::base_url().to_string(),
        }
    }

    fn set_health(&self, app: &AppHandle, state: BackendHealth) {
        let mut guard = self.health.lock().unwrap();
        if *guard != state {
            *guard = state;
            let _ = app.emit("tars://backend-health", state);
        }
    }

    fn set_relationship(&self, app: &AppHandle, ownership: Ownership, detail: &str) {
        let changed = {
            let mut guard = self.ownership.lock().unwrap();
            let changed = *guard != ownership;
            *guard = ownership;
            changed
        };
        let detail_changed = {
            let mut guard = self.detail.lock().unwrap();
            let changed = *guard != detail;
            *guard = detail.to_string();
            changed
        };
        if changed || detail_changed {
            eprintln!("[TARS][supervisor] {ownership:?}: {detail}");
            let _ = app.emit("tars://backend-status", self.status());
        }
    }

    fn owner_id(&self) -> Option<String> {
        self.owner.lock().unwrap().as_ref().map(|o| o.owner_id.clone())
    }

    fn persist_owner(&self, record: Option<OwnerRecord>) {
        if let Some(path) = &self.owner_record_path {
            match &record {
                Some(rec) => {
                    if let Some(parent) = path.parent() {
                        let _ = std::fs::create_dir_all(parent);
                    }
                    if let Ok(json) = serde_json::to_string(rec) {
                        if let Err(e) = std::fs::write(path, json) {
                            eprintln!("[TARS][supervisor] could not persist the owner record: {e}");
                        }
                    }
                }
                None => {
                    let _ = std::fs::remove_file(path);
                }
            }
        }
        *self.owner.lock().unwrap() = record;
    }

    /// Starts the background supervising thread. Call once, from `.setup()`.
    pub fn start(self: &Arc<Self>, app: AppHandle) {
        let this = Arc::clone(self);
        std::thread::spawn(move || this.run(app));
    }

    fn run(&self, app: AppHandle) {
        self.reconcile(&app);

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
            self.reconcile(&app);
        }
    }

    /// One pass of the policy: look at the port and act on what is there.
    fn reconcile(&self, app: &AppHandle) {
        self.reap_exited_child();
        let probe = probe_backend();
        if self.intentional_stop.load(Ordering::SeqCst) {
            return;
        }
        let our_id = self.owner_id();
        match decide(&probe, our_id.as_deref()) {
            Decision::Own => {
                self.note_healthy(app);
                let reclaimed = self.child.lock().unwrap().is_none();
                let detail = if reclaimed {
                    "this shell's own backend, reclaimed from an earlier run"
                } else {
                    "started and supervised by this shell"
                };
                self.set_relationship(app, Ownership::Owned, detail);
            }
            Decision::Attach => {
                self.note_healthy(app);
                self.set_relationship(
                    app,
                    Ownership::Attached,
                    "a compatible backend started outside this shell; TARS uses it and will leave it running",
                );
            }
            Decision::Conflict(detail) => {
                self.set_health(app, BackendHealth::Degraded);
                self.set_relationship(app, Ownership::Conflict, &detail);
            }
            Decision::Replace => {
                eprintln!("[TARS][supervisor] our own backend speaks an older contract; replacing it");
                self.stop_owned();
                self.spawn_and_wait_ready(app);
            }
            Decision::Wait => self.handle_missed_poll(app),
            Decision::Spawn => {
                if self.child_is_alive() {
                    // Our process is up but has not bound the port, or has
                    // lost it. Give it the same grace as a slow answer.
                    self.handle_missed_poll(app);
                } else {
                    self.handle_unexpected_down(app);
                }
            }
        }
    }

    fn note_healthy(&self, app: &AppHandle) {
        self.consecutive_failures.store(0, Ordering::SeqCst);
        self.missed_polls.store(0, Ordering::SeqCst);
        self.set_health(app, BackendHealth::Ready);
    }

    fn child_is_alive(&self) -> bool {
        self.child.lock().unwrap().is_some()
    }

    fn reap_exited_child(&self) {
        let mut guard = self.child.lock().unwrap();
        if let Some(child) = guard.as_mut() {
            if matches!(child.try_wait(), Ok(Some(_))) {
                *guard = None;
            }
        }
    }

    /// One slow answer is not a crash. Only our own live child can be hung
    /// from our point of view, and only it may be killed for it.
    fn handle_missed_poll(&self, app: &AppHandle) {
        if !self.child_is_alive() {
            // A backend we already know may simply be busy. An unknown
            // listener that will not answer can only be reported.
            let known = matches!(*self.ownership.lock().unwrap(), Ownership::Owned | Ownership::Attached);
            if !known {
                self.set_health(app, BackendHealth::Degraded);
                self.set_relationship(
                    app,
                    Ownership::Conflict,
                    "something holds the port but does not answer, and this shell did not start it",
                );
            }
            return;
        }
        let misses = self.missed_polls.fetch_add(1, Ordering::SeqCst) + 1;
        if misses < MAX_MISSED_POLLS {
            return;
        }
        eprintln!("[TARS][supervisor] our backend missed {misses} polls; treating it as hung");
        self.missed_polls.store(0, Ordering::SeqCst);
        self.kill_child();
        self.handle_unexpected_down(app);
    }

    fn kill_child(&self) {
        if let Some(mut child) = self.child.lock().unwrap().take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }

    fn handle_unexpected_down(&self, app: &AppHandle) {
        let failures = self.consecutive_failures.fetch_add(1, Ordering::SeqCst) + 1;
        if !should_attempt_restart(failures) {
            eprintln!(
                "[TARS][supervisor] backend failed {failures} consecutive times; giving up auto-restart, reporting DEGRADED"
            );
            self.set_health(app, BackendHealth::Degraded);
            self.set_relationship(app, Ownership::None, "the backend would not stay up; use Restart TARS to try again");
            return;
        }
        self.set_health(app, BackendHealth::Restarting);
        let delay = backoff_delay(failures);
        eprintln!("[TARS][supervisor] backend down (failure {failures}); restarting in {delay:?}");
        std::thread::sleep(delay);
        // The port may have been taken during the backoff. Spawning anyway
        // would create a process that can only fail to bind.
        if matches!(probe_backend(), Probe::Absent) {
            self.spawn_and_wait_ready(app);
        }
    }

    fn spawn_and_wait_ready(&self, app: &AppHandle) {
        self.set_health(app, BackendHealth::Starting);
        if self.launch.program.as_os_str().is_empty() {
            self.set_health(app, BackendHealth::Degraded);
            self.set_relationship(
                app,
                Ownership::None,
                "no backend runtime was found for this install, and nothing is serving the port",
            );
            return;
        }
        let record = OwnerRecord::generate();
        // Written before the spawn: a crash in between must not leave a
        // process nobody can prove they own.
        self.persist_owner(Some(record.clone()));
        match self.spawn_child(&record) {
            Ok(child) => {
                *self.child.lock().unwrap() = Some(child);
            }
            Err(e) => {
                eprintln!("[TARS][supervisor] failed to spawn backend: {e}");
                self.persist_owner(None);
                self.set_health(app, BackendHealth::Degraded);
                self.set_relationship(app, Ownership::None, &format!("could not start the backend: {e}"));
                return;
            }
        }
        let deadline = Instant::now() + READY_WAIT_TIMEOUT;
        while Instant::now() < deadline {
            if let Probe::Tars(identity) = probe_backend() {
                if is_compatible(&identity) && identity.owner_id == record.owner_id {
                    self.note_healthy(app);
                    self.set_relationship(app, Ownership::Owned, "started and supervised by this shell");
                    return;
                }
            }
            self.reap_exited_child();
            if !self.child_is_alive() {
                eprintln!("[TARS][supervisor] the backend exited before becoming healthy");
                return;
            }
            std::thread::sleep(Duration::from_millis(500));
        }
        eprintln!("[TARS][supervisor] backend did not become healthy within {READY_WAIT_TIMEOUT:?}");
    }

    fn spawn_child(&self, record: &OwnerRecord) -> std::io::Result<Child> {
        let mut cmd = Command::new(&self.launch.program);
        cmd.arg("run.py")
            .current_dir(&self.launch.working_dir)
            .env("BACKEND_PORT", backend_endpoint::port().to_string())
            .env("TARS_SUPERVISOR_OWNER_ID", &record.owner_id)
            .env("TARS_SUPERVISOR_SECRET", &record.secret)
            .envs(self.launch.envs.iter().map(|(k, v)| (k, v)));
        // An installed app has no console, so without this a backend that
        // fails to start leaves nothing to diagnose it from.
        if let Some(path) = &self.launch.log_file {
            if let Some(parent) = path.parent() {
                let _ = std::fs::create_dir_all(parent);
            }
            if let Ok(log) = std::fs::File::create(path) {
                if let Ok(err) = log.try_clone() {
                    cmd.stdout(Stdio::from(log)).stderr(Stdio::from(err));
                }
            }
        }
        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        cmd.spawn()
    }

    /// Cooperative stop of a backend we own: POST the graceful-shutdown
    /// endpoint with our secret, wait a bound for it to go, and force-kill
    /// only our own child handle as a last resort.
    fn stop_owned(&self) {
        let record = self.owner.lock().unwrap().clone();
        let owner_id = record.as_ref().map(|r| r.owner_id.clone());
        if let Some(record) = &record {
            let _ = request_graceful_shutdown(&record.secret);
        }
        let deadline = Instant::now() + GRACEFUL_SHUTDOWN_TIMEOUT;
        while Instant::now() < deadline {
            match probe_backend() {
                Probe::Tars(identity) if Some(&identity.owner_id) == owner_id.as_ref() => {}
                Probe::Unresponsive => {}
                _ => break,
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
        drop(guard);
        self.persist_owner(None);
    }

    /// Stop what is ours. Sets `intentional_stop` FIRST so the supervising
    /// loop never treats this exit as a crash. A backend we do not own is
    /// left exactly as it was: not asked to shut down, not killed.
    fn stop_inner(&self, app: &AppHandle) {
        self.intentional_stop.store(true, Ordering::SeqCst);
        let ownership = *self.ownership.lock().unwrap();
        let owned = ownership == Ownership::Owned || self.child_is_alive();
        if owned {
            self.stop_owned();
            self.set_relationship(app, Ownership::None, "stopped");
        } else {
            eprintln!("[TARS][supervisor] leaving the backend running: this shell does not own it ({ownership:?})");
        }
        self.set_health(app, BackendHealth::Degraded);
    }

    pub fn stop(&self, app: &AppHandle) {
        self.stop_inner(app);
        self.intentional_stop.store(false, Ordering::SeqCst);
    }

    /// Deterministic restart: stop (cooperative, bounded) then spawn fresh.
    /// Never loses persisted state (watchlist/daily-brief date live in the
    /// backend's own sqlite db, untouched by this process-level restart).
    /// A backend we do not own is not ours to restart, so this only looks
    /// again at what is on the port.
    pub fn restart(self: &Arc<Self>, app: &AppHandle) {
        self.stop_inner(app);
        self.consecutive_failures.store(0, Ordering::SeqCst);
        if matches!(probe_backend(), Probe::Absent) {
            self.spawn_and_wait_ready(app);
        }
        self.intentional_stop.store(false, Ordering::SeqCst);
        self.reconcile(app);
    }
}

/// What is on the port. Separates "nothing there" from "something there that
/// will not talk to us", because only the first means the port is ours to use.
fn probe_backend() -> Probe {
    match ureq::get(&backend_endpoint::url(HEALTH_PATH)).timeout(Duration::from_secs(2)).call() {
        Ok(response) if response.status() == 200 => match response.into_string() {
            Ok(body) => classify_health_body(&body),
            Err(_) => Probe::Unresponsive,
        },
        Ok(response) => Probe::Foreign(format!("answers the health path with HTTP {}", response.status())),
        Err(ureq::Error::Status(code, _)) => Probe::Foreign(format!("answers the health path with HTTP {code}")),
        Err(ureq::Error::Transport(_)) => {
            let address = std::net::SocketAddr::from(([127, 0, 0, 1], backend_endpoint::port()));
            match std::net::TcpStream::connect_timeout(&address, Duration::from_millis(500)) {
                Ok(_) => Probe::Unresponsive,
                Err(_) => Probe::Absent,
            }
        }
    }
}

fn request_graceful_shutdown(secret: &str) -> bool {
    ureq::post(&backend_endpoint::url(SHUTDOWN_PATH))
        .set("X-TARS-Supervisor-Secret", secret)
        .timeout(Duration::from_secs(3))
        .send_bytes(b"{}")
        .is_ok()
}

fn request_resume_rebase() -> bool {
    ureq::post(&backend_endpoint::url(REBASE_PATH))
        .timeout(Duration::from_secs(5))
        .send_bytes(b"{}")
        .is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn identity(service: &str, contract: i64, owner: &str) -> BackendIdentity {
        BackendIdentity {
            service: service.to_string(),
            contract_version: contract,
            instance_id: "i".to_string(),
            owner_id: owner.to_string(),
            build_id: "b".to_string(),
        }
    }

    #[test]
    fn nothing_on_the_port_means_spawn() {
        assert_eq!(decide(&Probe::Absent, Some("me")), Decision::Spawn);
    }

    #[test]
    fn our_own_compatible_backend_is_owned() {
        let probe = Probe::Tars(identity(EXPECTED_SERVICE, EXPECTED_CONTRACT, "me"));
        assert_eq!(decide(&probe, Some("me")), Decision::Own);
    }

    #[test]
    fn a_compatible_backend_we_did_not_start_is_attached_never_owned() {
        let probe = Probe::Tars(identity(EXPECTED_SERVICE, EXPECTED_CONTRACT, "someone-else"));
        assert_eq!(decide(&probe, Some("me")), Decision::Attach);
        assert_eq!(decide(&probe, None), Decision::Attach);
    }

    #[test]
    fn an_empty_owner_id_can_never_prove_ownership() {
        // An unsupervised backend reports "". Matching that against an empty
        // record would hand us the right to stop a stranger's process.
        let probe = Probe::Tars(identity(EXPECTED_SERVICE, EXPECTED_CONTRACT, ""));
        assert_eq!(decide(&probe, Some("")), Decision::Attach);
    }

    #[test]
    fn our_own_old_contract_backend_is_replaced() {
        let probe = Probe::Tars(identity(EXPECTED_SERVICE, EXPECTED_CONTRACT + 1, "me"));
        assert_eq!(decide(&probe, Some("me")), Decision::Replace);
    }

    #[test]
    fn someone_elses_incompatible_backend_is_a_conflict_and_is_left_alone() {
        let probe = Probe::Tars(identity(EXPECTED_SERVICE, EXPECTED_CONTRACT + 1, "other"));
        assert!(matches!(decide(&probe, Some("me")), Decision::Conflict(_)));
    }

    #[test]
    fn a_foreign_process_is_a_conflict() {
        assert!(matches!(decide(&Probe::Foreign("x".into()), Some("me")), Decision::Conflict(_)));
    }

    #[test]
    fn an_unresponsive_listener_is_not_treated_as_a_free_port() {
        assert_eq!(decide(&Probe::Unresponsive, Some("me")), Decision::Wait);
    }

    #[test]
    fn a_200_without_identity_is_foreign() {
        assert!(matches!(classify_health_body(r#"{"status":"ok"}"#), Probe::Foreign(_)));
        assert!(matches!(classify_health_body("<html>hello</html>"), Probe::Foreign(_)));
        assert!(matches!(classify_health_body(r#"{"identity":"tars-backend"}"#), Probe::Foreign(_)));
        assert!(matches!(classify_health_body(r#"{"identity":{}}"#), Probe::Foreign(_)));
    }

    #[test]
    fn another_service_declaring_an_identity_is_foreign() {
        let body = r#"{"identity":{"service":"nginx","contract_version":1}}"#;
        assert!(matches!(classify_health_body(body), Probe::Foreign(_)));
    }

    #[test]
    fn a_tars_identity_is_read_from_the_real_payload_shape() {
        let body = r#"{"status":"ok","identity":{"service":"tars-backend","contract_version":1,
            "instance_id":"abc","owner_id":"me","pid":12,"build_id":"f7","build_source":"git",
            "app_version":"1.0.0","started_at":"2026-10-31T00:00:00+00:00"}}"#;
        match classify_health_body(body) {
            Probe::Tars(identity) => {
                assert_eq!(identity.owner_id, "me");
                assert!(is_compatible(&identity));
            }
            other => panic!("expected a TARS identity, got {other:?}"),
        }
    }

    #[test]
    fn the_owner_record_round_trips_through_disk() {
        let dir = std::env::temp_dir().join(format!("tars-owner-test-{}", uuid::Uuid::new_v4().simple()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("backend-owner.json");
        let record = OwnerRecord::generate();
        std::fs::write(&path, serde_json::to_string(&record).unwrap()).unwrap();
        assert_eq!(read_owner_record(&path), Some(record));
        std::fs::write(&path, "not json").unwrap();
        assert_eq!(read_owner_record(&path), None);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn generated_owner_records_are_unique_and_keep_the_secret_separate() {
        let (a, b) = (OwnerRecord::generate(), OwnerRecord::generate());
        assert_ne!(a.owner_id, b.owner_id);
        assert_ne!(a.owner_id, a.secret);
    }

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
