# Handoff — Claude Code

Owned directory: `apps/backend/`. Only Claude Code edits this file. See
[AGENTS.md](../../../AGENTS.md) for the full handoff protocol.

Update this file at the end of every session working on `apps/backend/`, using
the template below. Keep only the latest handoff at the top; older entries
may be kept below a `---` separator if useful for history, but are not
required reading for the next session (`CURRENT_STATE.md` is authoritative
for that).

---

## Latest handoff — Alexa-speed hot-state, Phase A: latency instrumentation (2026-08-20)

**Branch**: `feature/realtime-hot-state`
**Worktree**: `C:\TARS-worktrees\realtime-hot-state` (dedicated, per the
task's own isolation requirement — do not confuse with the main
`c:\TARS-Overnight-Integration` checkout, which stays on
`feature/trading-intelligence-architecture`)
**Base SHA**: `17713e4` (tip of `feature/trading-intelligence-architecture`
at session start — includes the Claude/Codex provider work, trading-
intelligence fixes, and skill system)
**Final SHA**: `aa21d8a`

**Ownership note**: this is a multi-phase, cross-stack feature ("TARS
should begin a meaningful chart response in ~5s instead of 15-25s")
explicitly directed at one agent building the full stack in an isolated
worktree, specifically so it doesn't collide with other agents' concurrent
sessions on the shared repo. `apps/web/` (including `src-tauri/`) is
normally Antigravity's lane per `AGENTS.md` — later phases of this same
effort (background chart watcher, native capture) will touch
`apps/web/src-tauri/*.rs` and `apps/web/src/*`; each such change will be
called out explicitly in this file for review before any merge. This
Phase A session touched one frontend file
(`apps/web/src/runtime/ChartAnalysisClient.ts`, one additive line — see
below); everything else is `apps/backend/`. `contracts/*.schema.json` was
not touched.

**Work completed** — Phase A only (baseline latency instrumentation; the
task requires measuring a real baseline before changing behavior, not
optimizing on anecdote): the codebase already had two pieces of latency
infrastructure that were built but never actually wired up —
`ProviderDiagnostics` (`assistant/provider.py`, added by an earlier
session) was computed on every provider call but discarded immediately
(`assistant/router.py`'s `_call_provider` built it then only used
`reply.text`/`reply.provider`), and `app/latency.py`'s `LatencyTracker` was
exercised only by its own unit tests, never called from a live request
path. Phase A closes that gap rather than inventing a third timing
mechanism:

1. **`storage/migrations/0005_request_traces.sql`** — new
   `request_traces` table: one row per chart-analysis or text-chat
   request, with `capture_ms`/`provider_start_ms`/`first_token_ms`/
   `provider_latency_ms`/`total_ms`/`error`, indexed on `(kind,
   started_at)`.
2. **`app/latency_store.py`** (new) — `LatencyTraceStore`, the same
   `*Service`-wrapping-the-shared-`aiosqlite`-connection pattern every
   other service in this backend uses (`EventService`,
   `ConversationStore`, etc.). `record()` writes a trace;
   `percentiles(kind)` computes P50/P90/P95/max/error-count in Python
   (linear-interpolation percentile, since SQLite has no
   `PERCENTILE_CONT`).
3. **`assistant/chart_analysis.py`** — `ChartAnalysisService` takes an
   optional `trace_store` (default `None`, so every existing caller/test
   keeps working unchanged). `analyze_stream()` now records a trace on
   every exit path: success (provider name, `claude_start_ms` as
   `provider_start_ms`, `first_token_ms`, total), image-decode failure,
   and any provider exception — never only the happy path, since the
   whole point is real baseline numbers including failures.
4. **`assistant/router.py`** — `AssistantRouter` takes the same optional
   `trace_store`; `_call_provider()` now persists `reply.diagnostics`
   (finally using the field that was always being computed) on both
   success and `AssistantProviderError` failure.
5. **`app/routers/diagnostics.py`** (new) —
   `GET /api/v1/diagnostics/latency?kind=chart_analysis&limit=200`,
   same exposure level as the existing `/api/v1/runtime/readiness`
   (developer-facing, not called by the HUD/voice UI, no secrets).
6. **Capture-stage timing threaded end to end**: the frontend already
   computed `captureMs` (hide/DWM-wait/BitBlt/restore) locally for its own
   console `[PERF][chart]` marks but never sent it to the backend —
   `ChartAnalysisClient.ts` now includes `capture_ms` in the POST body
   (one additive line), `app/routers/assistant.py`'s
   `analyze_chart_stream` reads it, and it flows into the trace row. This
   is the one frontend file this session touched.
7. Wiring: `app/main.py` constructs one `LatencyTraceStore` off the
   shared DB connection at startup and passes it into both
   `ChartAnalysisService` and (via `app/deps.py`'s
   `get_assistant_router`) `AssistantRouter`.

**Files changed**: `apps/backend/storage/migrations/0005_request_traces.sql`
(new), `apps/backend/app/latency_store.py` (new),
`apps/backend/app/routers/diagnostics.py` (new),
`apps/backend/app/deps.py`, `apps/backend/app/main.py`,
`apps/backend/app/routers/assistant.py`,
`apps/backend/assistant/chart_analysis.py`,
`apps/backend/assistant/router.py`,
`apps/backend/tests/test_chart_analysis.py` (added streaming-trace cases),
`apps/backend/tests/test_latency_store.py` (new),
`apps/backend/tests/test_latency_trace_wiring.py` (new),
`apps/backend/tests/test_diagnostics_router.py` (new),
`apps/web/src/runtime/ChartAnalysisClient.ts` (one additive line).

**Tests run**: full `apps/backend` suite — **553 passed**, 0 failed, 0
regressions. `ruff check` on every touched/created file — clean (3
findings auto-fixed: unnecessary quoted forward-refs now that
`from __future__ import annotations` makes them unneeded, one unsorted
import block). `mypy --config-file pyproject.toml app assistant` —
clean, same 7 pre-existing errors as before this session (in
`assistant/providers/{gemini,codex,claude_code}.py` and
`skill_registry/db.py` — none in any file this session touched).
Frontend change **not** type-checked with `tsc` — `apps/web/node_modules`
is not installed in this worktree (consistent with prior sessions'
handoffs noting the same gap); the change itself is a single additive
object-literal field with no type surface change, low risk, but flagging
per this repo's honesty convention rather than claiming a check that
didn't run.

**Known limitations / deliberately deferred**:

- **No live P50/P90/P95 numbers from a real running app yet.** This
  session proves the plumbing (trace rows are written correctly on every
  exit path, percentiles compute correctly — see the new test files) but
  has not run the actual `claude` CLI against a real captured chart N
  times to produce real baseline numbers. That requires either the user's
  own TradingView session or a live backend + Tauri app running
  end-to-end, which this session didn't have. The commit-message numbers
  already in this repo's history (`8ce9ea1`, `9159069`: 10-31s, model
  inference dominates) remain the best evidence until a live run happens
  — Phase H's benchmark harness is the right place to automate that,
  though a spot real-world check earlier would sharpen Phase B-D's exact
  threshold choices.
- **`respond_stream()` (both `ClaudeCodeProvider` and any future
  provider) still doesn't emit `ProviderDiagnostics`** — only the
  non-streaming `respond()` does. The chart-analysis streaming path's
  trace instead uses its own `claude_start_ms`/`first_token_ms`/
  `complete_ms` marks (already existed, just now persisted), which cover
  the same ground for that path specifically. If a future phase wants
  richer diagnostics (exit code, request_id, model name) on the streaming
  path too, `respond_stream`'s `complete` event would need an additive
  `diagnostics` field — not done here to keep this phase additive-only
  and scoped to persistence, not provider-adapter changes.
- **General-chat streaming (`handle_text_stream`) is not traced** — only
  `handle_text`'s non-streaming `_call_provider()` path is. The streaming
  chat path's provider branch doesn't produce a `ProviderDiagnostics`
  either (see above), so there was nothing to persist yet; adding a
  timing-only trace there without diagnostics felt like it would produce
  a real-but-thin row that invites over-reading. Left for whoever wires
  streaming diagnostics.
- **Dev-endpoint exposure, not access-controlled** — matches
  `/api/v1/runtime/readiness`'s existing exposure level (this app binds
  to `127.0.0.1` by default per `ARCHITECTURE.md`); no new gating pattern
  invented.

**Exact dependencies required from other agents**: none blocking. Later
phases of this same effort (B: HotChartState model/persistence; C:
BackgroundChartWatcher, which needs new Rust capture code in
`apps/web/src-tauri/`) will cross into Antigravity's normal ownership —
flagged in advance here per `AGENTS.md`'s "flag it in your handoff instead"
guidance, not attempted without disclosure.

**Next recommended action**: Phase B (HotChartState domain model +
`0006_hot_chart_state.sql`, purely additive, no behavior change) on the
same branch/worktree. Full plan (all 8 phases) is tracked outside this
repo in the originating conversation; ping the coordinator if a written
copy in `docs/coordination/` would help future sessions pick this up
independently.

---

## Latest handoff — Wave 2B: Windows context + desktop-control skills (2026-08-17)

**Branch**: `feature/wave2b-context-windows`
**Base SHA**: `877dde0bd5f93817b78566eb4c8e0f9a58106c4f` (`integration/wave2`,
post Wave 2A/M2A merge, ADR-022)
**Final SHA**: `644fbe4` (implementation+tests commit; this handoff and
`docs/coordination/wave2/m2b/claude.done.json` land in a follow-up
docs-only commit on the same branch — same convention as prior entries in
this file).

This is a **continuation of the Wave 2A architecture**, not a new one —
`User/Voice/HUD/Context -> ActionRequest -> Action Runtime -> Permission
Engine -> Skill Registry -> Windows skill -> ActionResult + Audit` is
unchanged. Nothing in this session bypasses the Action Runtime or
Permission Engine; there is no independent agent loop (Codex still owns
orchestration/control runtime).

**Work completed**:

1. **Rich active-window context** — `app.action_contracts.ActiveWindowContext`
   (and its mirror, `contracts/action-request.schema.json`'s
   `$defs/active_window_context`) gained four new **optional** fields:
   `window_state` (normal/minimized/maximized/unknown, via
   `win32gui.IsIconic`/`IsZoomed`), `monitor` (geometry + `is_primary`, via
   `win32api.MonitorFromWindow`/`GetMonitorInfo` — geometry only, no pixel
   data), `focused_control` (identity metadata only — control type/name/
   automation id/class name, **no value/content**, and `name` is omitted
   entirely when the focused control `IsPassword`), and `source`
   (`win32`/`ui_automation`, which capture path produced the context).
   This is purely additive — `additionalProperties: false` blocks were
   extended, not loosened, and `actions/requests.py`'s
   `_ALLOWED_CONTEXT_FIELDS`/`_ALLOWED_MONITOR_FIELDS`/
   `_ALLOWED_FOCUSED_CONTROL_FIELDS` allowlists moved in lockstep with the
   schema and Pydantic model, per the existing "these three things drift
   together or not at all" pattern the M2A explore report flagged.
   **Deliberately excluded**: `selected_text` and clipboard content are
   **not** fields on `ActiveWindowContext` — see point 4 below for why.
2. **Windows UI Automation layer** — new `skills/_desktop_automation.py`.
   Real UIA via the `uiautomation` package (comtypes-based COM interop,
   not a mock/simulation layer): `resolve_window()` (reuses
   `windows_app._find_window` for executable/title matching, or the
   foreground window for "current"), `control_from_hwnd()`
   (`auto.ControlFromHandle`), `walk_actionable_controls()` (bounded
   BFS — depth-capped, count-capped, and separately visit-capped so a
   pathological tree like File Explorer with thousands of items can't
   cause unbounded work), `serialize_control()` (type/name/automation-id/
   class-name/bounds/enabled/password-flag — `IsPassword` controls have
   `name` redacted to `null`), and the actual pattern-driving functions:
   `do_focus`/`do_invoke`/`do_type`/`do_select`/`do_scroll`, each calling
   a real UIA pattern (`InvokePattern`/`TogglePattern`/
   `LegacyIAccessiblePattern` for invoke; `ValuePattern`/
   `LegacyIAccessiblePattern` for type; `SelectionItemPattern`/
   `LegacyIAccessiblePattern` for select; `ScrollPattern` for scroll) —
   **no blind-coordinate clicking, no keystroke simulation**, per the spec.
   `read_selected_text()` uses `TextPattern.GetSelection()` and refuses
   (raises) on a password control rather than reading it.
   `read_clipboard_text()` uses `win32clipboard`'s `CF_UNICODETEXT` format
   only, returning `None` (not an error) when the clipboard holds no text.
3. **Control identity / `ControlHandleCache`** — UIA `Control` objects
   aren't JSON-serializable or independently re-derivable by pointer, so
   `list_controls`/`inspect_current_window` hand back an opaque
   `control_id` (a random `uuid4().hex` token, not a pointer/runtime-id
   encoding) that resolves through a small in-process cache: TTL 180s,
   capped at 400 entries (oldest evicted first on overflow). This is
   short-lived actionability state, not indefinite raw-UI-tree retention —
   satisfies the "do not retain raw UI trees indefinitely" requirement
   while still letting a follow-up `focus_control`/`invoke_control`/etc.
   target the exact element it was just shown. An unknown/expired
   `control_id` fails loudly (`SkillExecutionError` -> a real `FAILED`
   `ActionResult`, "call list_controls again"), never silently no-ops.
4. **`skills/desktop_control.py` (`DesktopControlSkill`)** — 9 actions:
   `inspect_current_window`, `list_controls`, `focus_control`,
   `invoke_control`, `type_into_control`, `select_control`,
   `scroll_control`, `read_selected_text`, `read_clipboard`.
   `inspect_current_window` **is** the "context snapshot" primitive from
   the spec (active app, window title, up to `max_controls` visible
   actionable controls [default 20, hard-capped 50], best-effort selected
   text, and clipboard text **only** when `include_clipboard=true` is
   explicitly passed) — bounded, ephemeral (returned in `ActionResult.data`,
   not cached server-side), and clipboard/selected-text are opt-in per
   call rather than silently attached, matching the spec's "clipboard
   context through an explicit read action" and "do not silently scrape...
   protected desktop content" requirements. `type_into_control` refuses
   `mode="append"` on a password field (reading its current value to
   concatenate would be exactly the scraping this is meant to prevent);
   `mode="replace"` is allowed (the user directing the assistant to type
   into a field they're looking at is a normal, one-directional action,
   not a read).
5. **Risk classification** — wired into `actions/permissions.py`'s
   `_KNOWN_ACTION_POLICY["desktop_control"]`: `inspect_current_window`/
   `list_controls`/`read_selected_text`/`read_clipboard` = `READ_ONLY`;
   `focus_control`/`scroll_control` = `LOW_RISK`; `invoke_control`/
   `type_into_control`/`select_control` = `CONFIRM_REQUIRED`. This is a
   **required** explicit policy, not a convenience one — the permission
   engine's generic verb-name fallback (`_STATE_CHANGING_ACTION` regex:
   run/execute/write/create/update/install/uninstall/move/rename/copy/
   send/post/upload/download) does **not** match `invoke_control`,
   `type_into_control`, or `select_control`, so without this entry those
   three would default to `READ_ONLY` — exactly the "typing/changing
   application state must NOT be READ_ONLY" failure mode the spec warned
   about. Verified directly (see Tests).
6. **`skills/registry.py`** — `desktop_control` added to the eager
   `SKILLS` dict (stateless aside from its own bounded control cache, same
   category as the other four DB-independent skills).

**Real Windows verification** (Windows 11, this sandbox — not mocked):
launched a real `notepad.exe` against a uniquely-named temp file (modern
Windows 11 Notepad is a single shared-process, multi-tab MSIX app — a
plain `Popen(...).kill()` on the launcher stub does **not** close the tab,
since the stub exits immediately after activating the shared host; test
teardown closes precisely that test's own tab via its real "Close Tab" UIA
button, never the shared process, so it can't disturb the user's other,
unrelated open Notepad tabs — confirmed by re-diffing the window list
before/after). Against that real window: `list_controls` found its actual
`DocumentControl` ("Text editor" / `RichEditD2DPT`) and its real "Add New
Tab" button (`AutomationId="AddButton"`); `type_into_control` genuinely
set the editor's text via `ValuePattern.SetValue` (confirmed via
`vp.Value` readback during exploration); `invoke_control` genuinely
clicked "Add New Tab" via `InvokePattern.Invoke()` and a real new tab
appeared (then cleaned up); `select_control` genuinely selected a real
`TabItemControl` via `SelectionItemPattern.Select()`; `scroll_control`
genuinely scrolled the editor via `ScrollPattern.Scroll()` after filling
it with 500 lines (confirmed `VerticallyScrollable=True` first);
`read_selected_text`/`read_clipboard` returned real, honest empty/present
state, not fabricated data. **A real bug was caught this way**: an early
draft called `win32gui.GetWindowThreadProcessId`, which doesn't exist —
that API is `win32process.GetWindowThreadProcessId` — and this only
surfaced because the exploration script crashed against a real window;
a mock would not have caught it. Fixed in
`skills/_desktop_automation.py`.

**Files changed**: `apps/backend/skills/_desktop_automation.py` (new),
`apps/backend/skills/desktop_control.py` (new),
`apps/backend/tests/test_skill_desktop_control.py` (new, 20 cases),
`apps/backend/skills/registry.py`, `apps/backend/actions/permissions.py`,
`apps/backend/actions/requests.py`, `apps/backend/app/action_contracts.py`,
`apps/backend/tests/test_skill_registry.py` (updated expected skill set),
`apps/backend/requirements.txt` (additive: `comtypes==1.4.16`,
`uiautomation==2.0.29`), `contracts/action-request.schema.json` (additive
schema extension — see point 1 above; this is normally
read-only-without-coordinator-sign-off per `AGENTS.md`, flagging
explicitly since the Wave 2B task itself directed this extension).

**Tests run** (from `apps/backend/`):

- `python -m pytest tests/test_skill_desktop_control.py -v` — **20
  passed** (real Notepad interaction, see above).
- Full suite, `python -m pytest -q` — **270 passed** on one run; a second
  full run showed **269 passed, 1 failed**
  (`test_skill_windows_app.py::test_execute_focus_finds_and_focuses_real_window`,
  `SetForegroundWindow` timing out — this is the **same pre-existing,
  environment-dependent Windows foreground-lock flake** Wave 2A's own
  `claude.done.json` already flagged for that exact test; nothing in this
  session touches `windows_app.py`, and the failure is intermittent based
  on which process last had OS input focus, not a regression).
- `python -m ruff check skills/ actions/ app/action_contracts.py
  tests/test_skill_desktop_control.py tests/test_skill_registry.py` —
  **clean** (two real `B023` loop-variable-capture findings caught and
  fixed with the standard default-argument binding pattern, not suppressed).
- `python -m mypy --config-file pyproject.toml skills` — **clean, 10
  source files** (one real finding fixed: a branched `Control | None` vs
  `Control` assignment in `_execute_read_selected_text`, restructured with
  an explicit `Control | None` local and a None-check before use).

**Known gaps**:

- **Rust/native-shell side is untouched by design.** `apps/web/src-tauri/`
  is Antigravity's ownership per `AGENTS.md`; `get_active_window_context()`
  there still only captures executable/title/bounds. The new
  `window_state`/`monitor`/`focused_control`/`source` schema fields are
  additive and safe to send from Rust once wired, but nothing currently
  populates them on the passive per-`ActionRequest` `active_context` path
  — only the backend's own `inspect_current_window` action (which queries
  live Windows state directly, independent of whatever the HUD attaches)
  populates them today. Wiring the Tauri side is a natural Antigravity
  follow-up, not attempted here (would cross ownership boundaries).
- **`invoke_control`'s InvokePattern/TogglePattern/LegacyIAccessiblePattern
  fallback chain does not include a coordinate-click fallback**, by design
  (spec: "do not use blind coordinates when a semantic UI Automation
  target exists") — a control that genuinely exposes none of those three
  patterns will fail honestly (`SkillExecutionError`) rather than falling
  back to a click simulation. Not exercised against such a control in this
  session (Notepad's controls all support at least one).
- **`type_into_control` has no keystroke-simulation fallback** — only
  `ValuePattern`/`LegacyIAccessiblePattern.SetValue`. A control that
  exposes neither (some custom-drawn/game-engine UI, rare in normal
  Windows apps) will fail honestly rather than simulating keypresses.
  Scope was intentionally kept to real UIA patterns, per the spec's
  semantic-target requirement.
- **`select_control`'s `LegacyIAccessiblePattern` fallback path
  (`SELFLAG_TAKESELECTION | SELFLAG_TAKEFOCUS`) was not exercised in this
  session** — Notepad's tabs all support `SelectionItemPattern` directly,
  so the fallback branch is implemented per the pattern's documented
  contract but not independently verified against a control that lacks
  `SelectionItemPattern`.
- **File Explorer and Settings were not verified this session** — Notepad
  covered enumeration/focus/invoke/type/select/scroll/read-text/
  read-clipboard end-to-end; File Explorer/Settings were reviewed as
  targets during design (both expose standard UIA trees) but not
  separately exercised. Recommend a follow-up targeted check if
  File-Explorer-specific quirks (e.g. its list-view virtualization) matter
  for a near-term use case.
- **No multi-step workflow orchestration was built**, intentionally — the
  spec is explicit that Codex owns orchestration/control runtime and this
  session must not create an independent agent loop. The primitives
  (`inspect_current_window`/`list_controls` for inspect+select-target,
  `focus_control` for focus, `invoke_control`/`type_into_control`/
  `select_control`/`scroll_control` for act, and re-calling
  `inspect_current_window`/`list_controls` for verify) are each
  independently dispatchable through the Action Runtime today; composing
  them into a "focus -> inspect -> select -> act -> verify" sequence is
  the orchestrator's job, not this skill's.
- **`SetForegroundWindow` foreground-lock flakiness** (see Tests) affects
  `focus_control` the same way it affects `windows_app.focus` — both call
  into UIA/win32 focus APIs that are subject to the same OS-level
  foreground-lock timeout under some process-focus histories. This is a
  pre-existing Windows OS behavior, not specific to this session's code.

**Exact dependencies required from other agents**:

- **Antigravity** (`apps/web/src-tauri/`): the schema now supports
  `window_state`/`monitor`/`focused_control`/`source` on `active_context`
  — populating them from the native shell (extending
  `get_active_window_context()` in `apps/web/src-tauri/src/lib.rs`) is
  optional/additive, not required, since the backend's own
  `inspect_current_window` action already provides this data on demand
  without depending on the HUD to supply it.
- **Codex** (`apps/backend/actions/` / orchestration): the five action
  primitives listed under "no multi-step workflow orchestration" above are
  ready to compose into a focus->inspect->select->act->verify sequence
  whenever Codex's control runtime needs one; each already goes through
  the existing Action Runtime/Permission Engine/confirmation-token flow
  unchanged.
- No blocking dependency for either — this session's additions are usable
  standalone via `POST /api/v1/actions` today.

**Next recommended action**: a coordinator decides whether this needs a
schema-version bump (currently left at `1.0.0` since the change is
strictly additive/backward-compatible — no existing consumer breaks) or
just an ADR note recording the additive extension, same treatment as
other additive contract changes in this repo's history. Antigravity can
independently pick up wiring `monitor`/`window_state`/`focused_control`
into the Tauri side whenever convenient. A follow-up session could verify
File Explorer/Settings specifically and exercise the
`LegacyIAccessiblePattern` fallback paths against a control that lacks the
primary pattern.

---

## Latest handoff — Wave 2A M2A Phase 2: skills execution layer (2026-08-17)

**Branch**: `feature/wave2-core-skills`
**Worktree**: `C:\TARS-Wave2-Claude` (do not confuse with the V1 worktrees above)
**Base SHA**: `20f2353e100a37d93621a018e72951038a50585c` (`integration/wave2`)
**Final SHA**: `f900293454afae300a64e177a142e6f86f719917` (implementation+tests
commit; this handoff/`docs/coordination/wave2/m2a/claude.done.json` are
committed in a follow-up docs-only commit on the same branch — see
`git log feature/wave2-core-skills` for its trailing SHA, same convention as
prior entries in this file: *Final SHA* names the last substantive code/test
commit, not the handoff-writing commit).

This is a **new milestone** (Wave 2A / M2A), not a continuation of the V1
backend work below — read
[`docs/coordination/wave2/M2A_SPEC.md`](../wave2/M2A_SPEC.md) and
[`M2A_INTERFACES.md`](../wave2/M2A_INTERFACES.md) first. `apps/backend/app/`,
`apps/backend/{events,voice,assistant,memory}/`, and all V1 tests are
**unmodified** except `apps/backend/requirements.txt` (one additive line,
`pywin32==312`).

**Work completed** — implemented `apps/backend/skills/`, the real Windows
skill-execution layer Codex's action runtime (`apps/backend/actions/`, not
yet built) will dispatch validated `ActionRequest`s into:

1. **`skills/windows_app.py` (`WindowsAppSkill`)** — `launch`
   (`subprocess.Popen` with an argv list, never `shell=True`; bare names
   resolved via `shutil.which`, absolute targets must be an existing
   `.exe` with no `..` segments), `focus` (real `pywin32`
   `win32gui`/`win32process`/`win32api` — enumerates visible windows,
   matches by executable basename or window-title substring, calls
   `ShowWindow`/`SetForegroundWindow`), `list_running` (read-only: exe name
   + window title only, no content). `pywin32==312` was already present
   and genuinely importable in this sandbox (`import win32gui,
   win32process, win32con, win32api` succeeds); added it to
   `requirements.txt` since it wasn't previously pinned there.
2. **`skills/filesystem.py` (`FilesystemSkill`)** — `list`/`search`
   (read-only, boundary-checked: `Path.resolve()` then
   `Path.relative_to()` against the caller's home-directory tree, so `..`
   traversal or an absolute path outside the allowlist is rejected before
   any filesystem access), `open` (`os.startfile`). **Write/delete/move
   are not implemented** — `classify_risk()` returns `BLOCKED` and
   `validate()` raises `SkillValidationError` for any such action; nothing
   silently no-ops.
3. **`skills/browser.py` (`BrowserSkill`)** — `open_url`/`search` via
   stdlib `webbrowser` only; `validate_http_url()` rejects any scheme
   other than `http`/`https` (`file://`, `javascript:`, `data:`, etc. all
   rejected before `webbrowser.open` is ever called). No headless
   automation/scraping.
4. **`skills/terminal.py` (`TerminalSkill`)** — `run_command` via
   `subprocess.run` with real captured stdout/stderr/exit code and a
   timeout (default 30s, hard-capped at 120s regardless of caller input).
   `classify_command()` is pure/deterministic: a conservative denylist
   (`format`, `del`/`rd`/`rmdir /s`, `Remove-Item -Recurse -Force`,
   `shutdown`/`Stop-Computer`/`Restart-Computer`, `diskpart`, `reg
   delete`, `net user`, `bcdedit`, `vssadmin`, `cipher /w`, `runas`,
   `sudo`, `takeown`, remote `iex`/`Invoke-Expression` combined with a
   fetch construct, writes targeting `C:\Windows\System32`) is checked
   **before** a narrow leading-verb `READ_ONLY` allowlist (`dir`, `ls`,
   `type`, `cat`, `echo`, `whoami`, `pwd`, `cd`, `Get-ChildItem`,
   `Get-Process`, `Get-Content`, `git status`/`log`/`diff`) — and any
   chaining/redirection operator (`&`, `&&`, `|`, `;`, `>`, `<`)
   disqualifies a command from that allowlist entirely, so `dir & del /s`
   classifies `BLOCKED` (via the denylist) rather than slipping through as
   `READ_ONLY` because it starts with `dir`. `validate()` re-checks
   `BLOCKED` independently (defense in depth), and `execute()` refuses to
   actually run a `BLOCKED` command even if called directly.
5. **`skills/obsidian.py` (`ObsidianSkill`)** — `search`/`read` wrapping
   the **existing** `memory.service.MemoryService` (FTS5 + vault
   indexing) — no reimplemented indexing/search. `search` delegates to
   `MemoryService.search(..., source="vault")` and preserves its
   `source`/`source_id` fields verbatim into `ActionResult.data`. `read`
   resolves a vault-relative path with the same boundary-check pattern as
   `filesystem.py` and reads the real file (`MemoryService` only indexes
   snippets, not full bodies, so this is the one piece not already
   exposed by the existing service).
6. **`skills/voice_bridge.py`** —
   `build_action_request_from_voice(text, *, source, active_context=None)
   -> ActionRequest | None`. A small, fixed set of 4 regex patterns
   (`focus <target>`, `open <http(s) url>`, `search for <query>`,
   `launch/open/start <app>`) mirroring `assistant/router.py`'s
   deterministic-routing pattern (M2A criterion 12). Returns `None` for
   anything unrecognized — the caller (voice pipeline / action runtime,
   not this module) falls through to the existing LLM/assistant path
   unchanged. `assistant/router.py` itself was **not modified**.
7. **`skills/registry.py`** — `SKILLS: dict[str, Skill]` (eager; the 4
   skills above with no live-DB dependency) and `build_registry
   (memory_service=None, vault_path=None) -> dict[str, Skill]` for the
   full 5-skill registry including `obsidian`. See "Interfaces exposed"
   below for exactly why `obsidian` is not in the eager `SKILLS` dict and
   how to get it.

**Interfaces exposed** (for Codex, per M2A_INTERFACES.md's "expected
surface between streams"):

- `from skills.registry import SKILLS` → `dict[str, Skill]` with keys
  `windows_app`, `filesystem`, `browser`, `terminal` — importable and
  constructible with zero side effects, safe to import at module load
  time.
- `from skills.registry import build_registry` →
  `build_registry(memory_service: MemoryService | None = None, vault_path:
  str | None = None) -> dict[str, Skill]`. Call this **once you have a
  live `MemoryService` instance** (e.g. `request.app.state.memory_service`
  / `app.deps.get_memory_service`, the same DI pattern `app/main.py`'s
  lifespan and `app/deps.py` already use for every other request-scoped
  singleton) to get the complete 5-skill registry including `obsidian`.
  `obsidian` is deliberately excluded from the eager `SKILLS` dict because
  `ObsidianSkill` wraps `MemoryService`, which wraps a live
  `aiosqlite.Connection` that this codebase only creates during app
  startup — constructing it at import time would mean either an
  unmanaged second DB connection or a fabricated stand-in, both wrong.
- All five skill classes subclass `app.action_contracts.BaseSkill`
  unmodified — `classify_risk()`/`validate()`/`execute()` match the
  frozen `Skill` Protocol exactly; every `execute()` return is built via
  `self._result(...)` (never a hand-built `ActionResult`).
- `skills.terminal.classify_command(command: str) -> RiskLevel` and
  `skills.browser.validate_http_url(url: str) -> str` and
  `skills.filesystem.resolve_within_safe_roots(path: str) -> Path` are
  exposed as standalone functions (not just methods) in case the action
  runtime wants to pre-classify/pre-validate without a full skill dispatch
  — not required, just available.

**Files changed**:
`apps/backend/skills/__init__.py` (new),
`apps/backend/skills/windows_app.py` (new),
`apps/backend/skills/filesystem.py` (new),
`apps/backend/skills/browser.py` (new),
`apps/backend/skills/terminal.py` (new),
`apps/backend/skills/obsidian.py` (new),
`apps/backend/skills/voice_bridge.py` (new),
`apps/backend/skills/registry.py` (new),
`apps/backend/tests/test_skill_windows_app.py` (new),
`apps/backend/tests/test_skill_filesystem.py` (new),
`apps/backend/tests/test_skill_browser.py` (new),
`apps/backend/tests/test_skill_terminal.py` (new),
`apps/backend/tests/test_skill_obsidian.py` (new),
`apps/backend/tests/test_skill_voice_bridge.py` (new),
`apps/backend/tests/test_skill_registry.py` (new),
`apps/backend/requirements.txt` (one additive line: `pywin32==312`).
No existing V1 file's behavior was touched;
`apps/backend/app/action_contracts.py` and `apps/backend/app/contracts.py`
were read but not edited (frozen, per `M2A_SPEC.md`).

**Tests run** (from `C:\TARS-Wave2-Claude\apps\backend`, targeted only, per
`M2A_SPEC.md`'s validation cadence for this milestone):

- `python -m pytest tests/test_skill_windows_app.py
  tests/test_skill_filesystem.py tests/test_skill_browser.py
  tests/test_skill_terminal.py tests/test_skill_obsidian.py -q` — **113
  passed**, 0 failed (the exact five files named in the Phase 2 task spec).
- Same command plus `tests/test_skill_voice_bridge.py
  tests/test_skill_registry.py` — **124 passed**, 0 failed.
- Regression spot-check of pre-existing modules this work imports from —
  `python -m pytest tests/test_action_contracts.py
  tests/test_memory_service.py tests/test_memory_fts.py
  tests/test_memory_vault.py tests/test_contracts.py -q` — **25 passed**,
  0 failed (unmodified, none weakened).
- `python -m ruff check skills/ tests/test_skill_*.py` — **clean**.
- `python -m mypy --config-file pyproject.toml skills` — **clean, 8
  source files**.
- Full `apps/backend/tests` suite was **not** run this session (targeted
  validation per milestone cadence, not repeated full-suite runs per
  stream — see `M2A_SPEC.md`).

**Known limitations** (see `claude.done.json`'s `known_gaps` for the full
text):

- `focus`'s `SetForegroundWindow` path was verified against a **real**
  win32 window created in-process during the test run (genuine pass, not
  mocked) — it was **not** verified against a separately-running foreground
  application (e.g. a real, independently-launched Notepad) in this
  sandbox session, since that would mean popping/interacting with a second
  process's GUI window during an automated run. The same win32 API calls
  apply regardless of which process owns the target window, so this is a
  reasonable-confidence gap, explicitly flagged rather than omitted —
  never claim this as fully end-to-end certified.
- `terminal`'s `execute()` shells out via `cmd.exe`
  (`subprocess.run(..., shell=True)`); PowerShell-only cmdlets in the
  `READ_ONLY` allowlist (`Get-ChildItem` etc.) classify correctly but will
  genuinely fail at execution time unless wrapped in
  `powershell -Command "..."` — an honest `FAILED` result with real
  stderr, not a silent lie, but worth the HUD/runtime knowing about.
- `filesystem`'s safe-root allowlist is intentionally just the home
  directory tree (`Path.home()`) — no mechanism yet to add more roots.
- `voice_bridge` recognizes exactly 4 phrase shapes by design (not a
  general NLU system).

**Exact dependencies required from other agents**:

- **Codex** (`apps/backend/actions/`): import `skills.registry.SKILLS` for
  the 4 DB-independent skills, and call
  `skills.registry.build_registry(memory_service=<its MemoryService
  instance>)` to get the full 5-skill registry including `obsidian` — the
  action runtime is the natural place to obtain a live `MemoryService` the
  same way `app/main.py`'s lifespan does today.
- **Codex**: the permission engine must be the actual enforcement point
  for `RiskLevel` — a skill's own `BLOCKED` handling (e.g. `terminal.py`
  refusing to run a `BLOCKED` command even if `execute()` is called
  directly) is defense-in-depth, not a substitute for the runtime
  re-deriving/checking the classification itself, per
  `M2A_INTERFACES.md`'s RiskLevel section.
- No blocking dependency on Antigravity's native shell for this phase.

**Next recommended action**: Codex wires `skills.registry.SKILLS` /
`build_registry()` into `apps/backend/actions/` dispatch. Once that exists,
recommend an end-to-end action-runtime→skills dispatch test as part of the
Phase 4 integration validation gate.

---

## Append-only event history fix (2026-08-16)

**Branch**: `fix/v1-final-lifecycle`
**Base SHA**: `9f13e09a7c44be27901d34cb885a63ceb8755463`
**Final SHA**: `bfb39df` (see `git log 9f13e09..bfb39df` for the full diff)

Not merged into `feature/v1-remediation-integration`, `integration/v1`, or
`main` — that remains a coordinator decision.

**Commits**:

| SHA | Summary |
|---|---|
| `224ea47` | Enforce append-only event persistence |
| `bfb39df` | Add duplicate/concurrency regression tests for event persistence |

**Work completed** — fixed the final backend certification blocker:
`trading_events` was not genuinely append-only.

**Root cause**: `EventService._persist` (`events/service.py`) wrote every
event with `INSERT OR REPLACE INTO trading_events ... `, keyed on
`event_id` (the table's `PRIMARY KEY`, unchanged from Wave 1). `event_id`
is a required, caller-suppliable field on the public
`POST /api/v1/events` route (`app/routers/events.py`) — the raw JSON body
is validated against the frozen contract and passed straight into
`TradingEvent(**raw)`, which accepts an explicit `event_id`. So any
resubmission of an existing `event_id` — a retried request, a buggy
upstream integration, or a malicious caller — silently overwrote
(destroyed) that historical row instead of being rejected. The mock
generator and `/dev/mock-event` path were never affected (they always let
the model's `default_factory=uuid4` mint a fresh id), and the invalidate
endpoint already minted a fresh id too (fixed in a prior session) — the
open path was specifically direct callers of `POST /api/v1/events`
supplying their own `event_id`.

**Persistence change** (`events/service.py`): switched `_persist` to a
plain `INSERT` and let the existing `PRIMARY KEY` constraint enforce
uniqueness at the database level (no schema/migration change needed — the
constraint was already there, just unenforced by the upsert). Added
`DuplicateEventError(RuntimeError)`, raised when SQLite reports
`IntegrityError` on the insert, carrying the offending `event_id`. Because
`record_event` calls `_persist` before `_apply_active_state`, a rejected
duplicate never reaches the `active_setups` write — no partial-state
corruption path exists structurally, not just by convention.

**Duplicate behavior**: `app/routers/events.py` catches
`DuplicateEventError` in all three event-ingestion routes
(`POST /api/v1/events`, `POST /api/v1/events/{id}/invalidate`,
`POST /api/v1/dev/mock-event`) and returns **HTTP 409** with
`{"detail": "Event <id> already exists"}` — no SQLite/DB internals in the
response. The original historical row is left byte-for-byte unchanged
(verified in tests below).

**Lifecycle verification**: `tests/test_event_service.py::test_lifecycle_three_events_stay_queryable_and_active_state_clears`
drives `SETUP_DEVELOPING -> SETUP_VALID -> SETUP_INVALIDATED` directly
against `EventService` and asserts three distinct `event_id`s, all three
rows still present in `get_history()`, and `get_active_setups()` empty
afterward. (The API-level version of this same lifecycle proof already
existed from a prior session:
`tests/test_events_api.py::test_invalidation_preserves_prior_history_as_distinct_events`
— left unmodified, still passing.)

**Concurrency verification**: `tests/test_event_service.py::test_concurrent_duplicate_event_id_only_one_persists`
fires two `record_event()` calls for the same `event_id` via
`asyncio.gather(..., return_exceptions=True)`. Because `app/db.py`
intentionally uses one shared `aiosqlite` connection (documented there as
the correct scope for this single-user local app, not a pool), concurrent
writers are serialized through that connection's single worker thread —
so the database's `PRIMARY KEY` constraint deterministically admits exactly
one writer and raises `IntegrityError`→`DuplicateEventError` for the
other, with no TOCTOU window (no check-then-insert; the constraint itself
is the check). Test asserts exactly one success, one `DuplicateEventError`,
and exactly one matching row in history afterward.

**Files changed**: `apps/backend/events/service.py`,
`apps/backend/app/routers/events.py`,
`apps/backend/tests/test_events_api.py` (added 2 cases),
`apps/backend/tests/test_event_service.py` (new — 4 cases). No schema
migration, no change to the frozen trading-event contract, no change to
already-passing routes/behavior outside the duplicate-id path.

**Tests run**:

- `python -m pytest apps/backend/tests` — **81 passed** (up from 75 at
  session start; existing tests unmodified except the 2 additions to
  `test_events_api.py`, none weakened).
- `ruff check apps/backend` — **clean**.
- `mypy --config-file apps/backend/pyproject.toml apps/backend` — **clean,
  57 source files**.

**Known limitations**: none identified for this specific blocker. The
concurrency proof is deterministic given this app's single-shared-connection
architecture (see `app/db.py`'s own docstring); if that architecture ever
changes to a real connection pool, the same `PRIMARY KEY`-constraint
approach still holds (SQLite enforces it regardless of which connection
issues the `INSERT`), so no follow-up is required on that account.

**Next recommended action**: re-run final certification against
`fix/v1-final-lifecycle`. No other backend work is pending from this
session.

---

## Certification remediation (2026-08-16)

**Branch**: `fix/v1-backend-cert-blockers`
**Base SHA**: `e8308d82361e6df3c0928c4994ac0505a2e4235f`
**Final SHA**: `910c05a` (see `git log e8308d8..910c05a` for the full diff)

**Commits** (each pushed after its own test/lint/type-check pass):

| SHA | Summary |
|---|---|
| `76ed328` | Event lifecycle: SETUP_INVALIDATED no longer overwrites SETUP_VALID history |
| `45b548b` | Payload security: real streaming body-size limit, not Content-Length-only |
| `11d975d` | Memory grounding: source_id now flows into assistant grounding context |
| `d478172` | Real voice path: STT->assistant->TTS proof test with actual local models |
| `d611723` | Ruff: resolved remaining `apps/backend/run.py` E402 violations |
| `910c05a` | This handoff |

Not merged into `feature/v1-integration`, `integration/v1`, or `main` —
that remains a coordinator/Codex decision.

**Work completed** — fixed five Codex-identified backend certification
blockers, scoped strictly to `apps/backend/` plus the two shared-test call
sites that encoded the bugs being fixed (see below):

1. **Event lifecycle / invalidation** (`app/routers/events.py`). The
   invalidate endpoint reused the original event's `event_id`; since
   `trading_events.event_id` is the primary key behind an
   `INSERT OR REPLACE` upsert, invalidating a setup silently destroyed its
   `SETUP_VALID` row. Fixed by letting the invalidated event get its own
   unique `event_id` (the model default), correlating back to the original
   via an `ORIGINAL_EVENT_ID:<uuid>` reason code (the frozen trading-event
   contract has no dedicated correlation field — not touched).
   Regression test: `tests/test_events_api.py::test_invalidation_preserves_prior_history_as_distinct_events`
   drives `SETUP_DEVELOPING -> SETUP_VALID -> SETUP_INVALIDATED` over the
   real HTTP API and asserts three distinct `event_id`s, all three still
   present in history, and active state cleared.
   `tests/acceptance/test_runtime.py::test_event_lifecycle_reaches_two_clients_and_persists`
   (Codex-owned, shared) asserted the old, buggy id-reuse behavior; updated
   it to match by symbol instead of a pre-known id (the new id is
   server-generated) and added an explicit "SETUP_VALID still in history"
   check. Verified against a live `python apps/backend/run.py` process with
   `TARS_ACCEPTANCE=1`.

2. **Payload security** (`app/body_limit.py`, `app/main.py`). The old
   middleware only compared the declared `Content-Length` header against
   1 MiB — a no-op for chunked transfer encoding, which never sends that
   header. Replaced with `MaxBodySizeMiddleware`, a raw ASGI middleware
   that counts bytes as uvicorn delivers them via `receive()` (the same
   representation for Content-Length- and chunked-framed bodies) and
   aborts as soon as the running total exceeds the cap, without ever
   buffering the oversized body. The abort signal (`RequestBodyTooLarge`)
   is a `BaseException`, not `Exception`, subclass so it can't be silently
   swallowed by a route's broad `except Exception` around body reading
   (confirmed this actually happens in `events.py`'s JSON-parse handler
   during testing — an `Exception` subclass got turned into an unrelated
   422). Verified against both `TestClient` and a live uvicorn process
   with a real chunked-transfer request (no `Content-Length` header sent).
   Regression tests: `tests/test_payload_limit.py` (normal payload passes,
   Content-Length-declared oversized payload rejected, chunked oversized
   payload rejected, chunked under-limit payload reaches the route).

3. **Memory grounding / source traceability** (`assistant/grounding.py`).
   `build_system_context` dropped `source_id` (the vault file path /
   conversation `message_id`) from retrieved FTS notes before handing them
   to the assistant provider — only the generic `source` type
   ("vault"/"conversation") survived, so a grounded answer could never be
   attributed to which specific note backed it. Fixed by passing
   `source_id` through and instructing the provider to cite it. No change
   to the frozen assistant-message contract (no field exists for
   memory-note citations; traceability lives in the grounding text, not
   response metadata — this was evaluated and doesn't rise to "genuine
   blocker" for a contract change). Regression tests:
   `tests/test_assistant_grounding.py` — `source_id` survives into the
   context payload sent to the provider (verified with a stub provider
   that echoes back what it received), and absent-information disclosure
   for both empty and irrelevant retrieval.

4. **Real voice backend path** (`tests/test_voice_end_to_end_real.py`).
   The existing implementation (`voice/pipeline.py`,
   `voice/pipecat_bridge.py`, `voice/pipecat_services.py`,
   `voice/providers/faster_whisper_stt.py`,
   `voice/providers/kokoro_tts.py`) was already architecturally correct —
   real STT -> `AssistantRouter` -> real TTS, no canned phrases — so no
   production code changed here. Added a proof test using actual model
   weights: Kokoro synthesizes "What setups require my attention?" into a
   real WAV fixture, faster-whisper independently transcribes it, the
   transcription (not the hardcoded phrase) is sent through
   `AssistantRouter` over the real `/api/v1/assistant/query` HTTP
   endpoint, and the reply is synthesized back to playable audio. A
   second, distinct control phrase must decode to distinct text — a
   bypassed/canned STT stage would return identical text regardless of
   audio, so the test fails in that case. **Executed with real models in
   this environment**: `faster-whisper==1.2.1` and `kokoro-onnx==0.5.0`
   installed successfully and both model weights downloaded and ran (CPU,
   `base`/`af_heart`). `pipecat-ai` (the realtime WebSocket voice session)
   was *not* installed/exercised this session — heavier dependency, and
   the proof requirement only concerns the STT->assistant->TTS data path,
   not the realtime transport; `voice/pipeline.py` etc. were reviewed by
   inspection only, not executed. `scipy` added to
   `requirements-voice.txt` as a test-only dependency (resamples Kokoro's
   24kHz output to whisper's 16kHz input).

5. **Ruff**: `apps/backend/run.py` had 2 pre-existing E402 violations
   (sys.path manipulation before `import uvicorn`/`app.config`, which is
   necessary when run as a script). Fixed with targeted `# noqa: E402` on
   those two lines rather than disabling the rule project-wide.
   `ruff check .` from `apps/backend/` is now clean.

**Files changed**: `apps/backend/app/main.py`, `apps/backend/app/body_limit.py`
(new), `apps/backend/app/routers/events.py`, `apps/backend/assistant/grounding.py`,
`apps/backend/run.py`, `apps/backend/requirements-voice.txt`,
`apps/backend/tests/test_events_api.py`, `apps/backend/tests/test_payload_limit.py`
(new), `apps/backend/tests/test_assistant_grounding.py` (new),
`apps/backend/tests/test_voice_end_to_end_real.py` (new); plus
`tests/acceptance/test_runtime.py` (Codex-owned — edited only to correct
an assertion that encoded the event-lifecycle bug being fixed; flagged
here rather than left silently unowned). This handoff file only under
shared coordination docs.

**Tests run**:

- `python -m pytest apps/backend/tests` — **75 passed, 0 skipped** (up
  from 61 passed/1 skipped at session start — the previously-optional
  `test_voice_real_adapters.py` cases now execute for real since
  faster-whisper/kokoro-onnx are installed in this environment).
- `ruff check .` (from `apps/backend/`) — **clean**.
- `mypy .` (from `apps/backend/`, using its own `pyproject.toml`) —
  **clean, 57 source files**. (Note: running `mypy apps/backend` from the
  repo root instead picks up no config and reports ~20 spurious
  `import-untyped`/`import-not-found` errors for packages the project
  deliberately leaves untyped/optional, e.g. `pipecat`, `openwakeword`,
  `jsonschema` — always run mypy from `apps/backend/` or with
  `--config-file apps/backend/pyproject.toml`.)
- `python -m pytest tests -q` (Codex's contract/unit suite) —
  **39 passed, 15 skipped, 1 failed**. The one failure
  (`tests/contracts/test_codegen_drift.py::test_generated_contracts_have_no_drift`)
  is a pre-environment gap unrelated to this session's changes: it shells
  out to `npm ci --prefix tools/codegen`, and `tools/codegen/node_modules`
  was never installed in this workspace. Confirmed pre-existing via
  `git stash` before investigating further — not a regression.
- Targeted acceptance tests against a manually-started live
  `python apps/backend/run.py` process with `TARS_ACCEPTANCE=1`:
  `test_event_lifecycle_reaches_two_clients_and_persists`,
  `test_websocket_disconnect_then_reconnect`,
  `test_unexpected_event_field_is_rejected`,
  `test_oversized_payload_is_rejected`, `test_no_live_trade_execution_surface`,
  `test_voice_reports_real_local_provider_path`,
  `test_assistant_refuses_to_invent_missing_trading_data`,
  `test_public_event_response_cannot_bypass_contract` — **all passed**.
  The *full* `tools/run_acceptance.py` suite (15/15) was not re-run:
  `apps/web/node_modules` is not installed in this workspace, and
  installing/running the frontend is out of scope for a backend-only
  remediation session per the task's "do not modify frontend files"
  instruction.

**Known limitations**:

- **Python version**: this machine has Python 3.13.3 and 3.7 installed,
  no 3.12 (`py -0p` confirms). All validation above ran under **3.13.3**.
  The project's `pyproject.toml` target (`py312`/mypy `python_version =
  "3.12"`) was left unchanged. This is *not* a certification of Python
  3.12 compatibility — someone with a 3.12 interpreter should re-run
  `pytest`/`ruff`/`mypy` before treating this as 3.12-verified.
  faster-whisper/kokoro-onnx/scipy installed successfully on 3.13 in this
  environment; that says nothing about 3.12 specifically.
- `pipecat-ai` (realtime voice WebSocket session) remains uninstalled and
  unexercised this session — `voice/pipeline.py`,
  `voice/pipecat_bridge.py`, `voice/pipecat_services.py` were reviewed by
  code inspection only, found architecturally correct (no canned
  phrases/faked success anywhere in the chain), but not run.
- `tools/codegen` npm deps and `apps/web` npm deps are both uninstalled in
  this workspace, so the codegen-drift test and the full black-box
  acceptance suite (which needs a live frontend) could not be run this
  session. Backend-only acceptance checks were run individually against a
  manually-started backend instead (see Tests run above).

**Exact dependencies required from other agents**: none — this was a
backend-only remediation pass against blockers Codex already identified.
If Codex re-certifies, the event-lifecycle broadcast for an invalidated
setup now carries a **new** `event_id` (not the original's); any external
consumer/tooling that assumed id-reuse (as `tests/acceptance/test_runtime.py`
did) should match by symbol instead, per the fix in commit `76ed328`.

**Next recommended action**: Codex re-certifies against
`fix/v1-backend-cert-blockers`. Before merging toward `integration/v1`,
someone should (a) validate under an actual Python 3.12 interpreter, (b)
install `apps/web` and `tools/codegen` npm deps and run the full
`tools/run_acceptance.py` 15-case suite plus the codegen-drift check, and
(c) optionally install `pipecat-ai` and exercise the realtime voice
WebSocket session directly (this session only proved the STT->assistant->TTS
data path, not the realtime transport around it).

---

## Wave 1 backend implementation (2026-08-16)

**Branch**: `feature/v1-backend-voice`
**Base SHA**: `ad6f4bf6bd4dcb5c4039450dc8b8540ce63108e7` (tip of `integration/v1` at session start)
**Final SHA**: `2bd64f664bf659085c67d3660ca21931ea06305a`

**Commits** (each pushed after its own test/lint/type-check pass):

| SHA | Summary |
|---|---|
| `f1fc132` | Core event backend: FastAPI + WebSocket + SQLite (Phase A) |
| `88fd015` | Assistant provider abstraction + deterministic routing (Phase B) |
| `1d7fda0` | Memory/retrieval layer: SQLite FTS5 + Obsidian vault indexing (Phase C) |
| `df2a9e1` | Voice pipeline: Pipecat + local STT/TTS/wake-word providers (Phase D) |
| `bea3c14` | Voice/assistant API surface + secure-by-default networking (Phase E+F) |
| `2bd64f6` | OpenTelemetry instrumentation (ADR-017) |

All six commits are on `feature/v1-backend-voice` and pushed to
`origin/feature/v1-backend-voice`. Nothing merged into `integration/v1` or
`main` — that is a coordinator decision per `AGENTS.md`.

## Work completed

Implemented the full backend mission (Phases A–F) plus OpenTelemetry
instrumentation from `TASKS.md`:

- **Phase A — Core event backend.** FastAPI app (`app/`), SQLite schema +
  migration runner (`storage/`), mock trading-event generator
  (`events/generator.py`), deterministic active-state calculation
  (`events/service.py`), WebSocket broadcast (`/ws/events`). Every event is
  validated directly against `contracts/trading-event.schema.json` via
  `jsonschema` (`app/contracts.py`) — not a hand-duplicated copy — so the
  backend can never silently drift from the frozen contract.
- **Phase B — Assistant tool layer.** `AssistantProvider` interface with
  four adapters: `MockAssistantProvider`, `ClaudeCodeProvider` (headless
  `claude -p --output-format json` CLI invocation, verified against current
  Claude Code docs, not assumed), `OllamaProvider` (local HTTP), optional
  `AnthropicAPIProvider` (paid, lazy-imports `anthropic`). Deterministic
  routing (`assistant/router.py`) resolves "show active setups" / "what
  requires my attention" straight from `EventService` — these never reach a
  model call. Everything else gets deterministic active-setup state as
  grounding text instructing the model never to invent trading facts.
  Conversation turns persist to `conversation_messages`, validated against
  `contracts/assistant-message.schema.json`.
- **Phase C — Memory.** `MemoryService` (`memory/`) with SQLite FTS5 full-
  text search over conversation memory and an indexed Obsidian vault
  (read-only indexer, content-hash-gated, excludes `.obsidian`/`.git`).
  Every search result carries `source`/`source_id` for traceability.
  Per ADR-013, `MemoryService` refuses to start with
  `SQLITE_VEC_ENABLED=true` rather than silently ignoring it — no
  measurement has justified semantic retrieval over FTS5 yet. APScheduler
  wired for periodic vault reindexing (housekeeping only, per ADR-016).
- **Phase D — Voice.** `WakeWordProvider`, `SpeechToTextProvider`,
  `TextToSpeechProvider` interfaces, each with a zero-dependency mock plus
  real local adapters: `FasterWhisperSTTProvider`, `KokoroTTSProvider`
  (kokoro-onnx, fully local), `FishSpeechTTSProvider` (HTTP client against
  a separately-run local Fish Speech API server), `OpenWakeWordProvider`.
  The realtime pipeline (`voice/pipeline.py`) is built on pipecat-ai
  1.7.0's current `PipelineWorker`/`WorkerRunner` API — verified against
  the installed package's actual module structure via introspection, not
  assumed from training data, since 1.7.0 deprecated the
  `PipelineTask`/`PipelineRunner` API shown in older Pipecat examples.
  Generic `ProviderBridgeSTTService`/`ProviderBridgeTTSService` wrap our
  own provider instances rather than Pipecat's vendor-specific services, so
  the realtime session and the one-shot REST endpoints share exactly one
  adapter instance per provider. Silero VAD needs no separate package —
  bundled inside `pipecat-ai` as an ONNX model.
- **Phase E — Voice/assistant API.** `POST /api/v1/voice/transcribe`
  (WAV → text), `POST /api/v1/voice/synthesize` (text → WAV),
  `GET /api/v1/voice/status` (provider readiness/names, never secrets),
  `WS /api/v1/voice/session` (realtime audio↔audio over the Phase D
  pipeline). Text question/response already existed from Phase B
  (`POST /api/v1/assistant/query`). STT/TTS/wake-word providers load in a
  background task (`app/voice_state.py`) so cold model downloads (minutes,
  see Benchmarks below) never block app startup; health/events/assistant
  work immediately.
- **Phase F — Local network.** `Settings.effective_host` resolves to
  `127.0.0.1` by default; `BIND_LAN=true` opts into `0.0.0.0`; an explicit
  `BACKEND_HOST` always wins. Tailscale Serve (the preferred private
  remote-access path, ADR-014) works against the loopback default without
  needing LAN exposure. `CORS_ALLOW_ORIGINS` is configurable (default `*`).
  `run.py` is the settings-aware uvicorn entrypoint. Verified with a real
  server smoke test: bound `127.0.0.1:8000`, `/api/v1/health` and
  `/api/v1/voice/status` both responded correctly.
- **OpenTelemetry (ADR-017).** `app/observability.py` configures a
  `TracerProvider` that logs spans through the stdlib logger by default (no
  network dependency) or exports to OTLP if `OTEL_EXPORTER_OTLP_ENDPOINT` is
  set. Instruments event ingestion, assistant requests (provider, latency,
  errors), and memory retrieval (retrieved `source:source_id`s). Verified
  end-to-end — spans logged correctly with no secrets in any attribute.

## Files changed

~80 files under `apps/backend/` (new: `app/`, `storage/`, `events/`,
`assistant/`, `memory/`, `voice/`, `tests/`, `run.py`, `README.md`,
`requirements*.txt`) plus additive edits to `.env.example` (root-level
config template — not under `docs/coordination/` or `contracts/`, and
already owns the backend env vars this session extended; edited
additively only, no existing key removed or renamed without a documented
reason). See the six commits above for the exact diff per phase.

## Interfaces exposed

**HTTP** (all under `/api/v1`):
- `GET /health`
- `GET /events`, `GET /events/active`, `POST /dev/mock-event`
- `POST /assistant/query`
- `GET /memory/search`, `POST /memory/reindex-vault`
- `POST /voice/transcribe`, `POST /voice/synthesize`, `GET /voice/status`

**WebSocket**:
- `/ws/events` — trading-event + active-setup broadcast
- `/api/v1/voice/session` — realtime voice (Pipecat pipeline)

**Python interfaces** (for future backend work, not external consumers):
`assistant.provider.AssistantProvider`, `voice.interfaces.WakeWordProvider`
/ `SpeechToTextProvider` / `TextToSpeechProvider`, `memory.service.MemoryService`.

## Environment/configuration

All new settings are documented in `.env.example` at the repo root and
`apps/backend/app/config.py` (kept in lockstep per that file's own
docstring rule). Defaults are the free/local-first path:
`ASSISTANT_PROVIDER=claude_code`, `STT_PROVIDER=faster_whisper`,
`TTS_PROVIDER=kokoro`, `WAKE_WORD_PROVIDER=mock` (see Known limitations),
`BIND_LAN=false`. Two new requirements files: `requirements-voice.txt`
(pipecat-ai + local model packages — optional, every voice provider mocks
without it) and `requirements-optional.txt` (paid-provider SDKs — never
required).

## Benchmarks (Phase D)

Measured in this sandboxed dev environment (CPU-only, no GPU):

| Adapter | Cold init (incl. first-run download) | Warm init | Generation latency |
|---|---|---|---|
| faster-whisper (`tiny`) | 48.98s | ~2.3s | 0.70s for 1s of audio |
| Kokoro TTS | 187.97s | ~2.5s | 8.66s cold / 0.77s warm, for a one-sentence reply |
| Fish Speech (local) | **not benchmarked** — see below | — | — |

**Fish Speech could not be benchmarked in this environment.** It has no
pip-installable inference API; running it requires cloning the
`fish-speech` repo, downloading GB-scale checkpoints, and running a
separate local API server (`tools/api_server.py`) this sandbox had no
practical way to host. `FishSpeechTTSProvider` is implemented as a
best-effort HTTP client against that server's documented `/v1/tts`
endpoint, but its exact request/response JSON schema was not fully
published where I could reach it — verify against the actual deployed
server version before relying on it. **Kokoro is the default `TTS_PROVIDER`
in `.env.example`** on this evidence: fully local, no separate process, and
fast once warm. If Fish Speech is benchmarked on real target hardware
(ideally with a GPU) and found to sound meaningfully better, flipping the
default is a one-line `.env` change — the provider is fully implemented.

## Tests

63 pytest cases across `apps/backend/tests/`, all passing; `ruff check .`
and `mypy app events storage assistant memory voice run.py` both clean.
Covers: contract validation, event persistence/active-state/invalidation,
WebSocket broadcast, deterministic assistant routing (proven to never call
the configured provider), assistant provider fallback-to-mock on
misconfiguration, memory FTS5 round-trip + vault indexing lifecycle, voice
mock providers, audio WAV↔PCM16 conversion, voice provider factory error
paths, network config resolution, and — the only tests touching real ML
models rather than mocks — `tests/test_voice_real_adapters.py`, which
exercises faster-whisper and Kokoro against actual (already-cached in this
environment) model weights; these skip cleanly via `pytest.importorskip`
if the voice extras aren't installed, so the rest of the suite needs no
network and no API keys.

**Not run**: no live audio round-trip through `/api/v1/voice/session` —
that needs a real microphone/browser client, which Antigravity owns. The
pipeline was verified by introspecting the installed `pipecat-ai` 1.7.0
package's real API (class names, constructor signatures, method
signatures) rather than assumed, and by the unit-level construction tests,
but not by an actual end-to-end voice conversation.

## Known limitations

- **No custom "TARS" wake-word model.** openWakeWord ships no pretrained
  model for the phrase "TARS" (only generic phrases like "hey jarvis").
  `OpenWakeWordProvider` is fully implemented and will run any
  custom-trained model pointed at by `WAKE_WORD_MODEL_PATH`, but training
  one is a separate, later effort (openWakeWord's own training pipeline,
  requiring assembled audio samples). `.env.example` defaults
  `WAKE_WORD_PROVIDER=mock` for this reason. Push-to-talk is unaffected —
  `/api/v1/voice/session` never requires wake-word detection to begin a
  session, satisfying AGENTS.md's "guaranteed regardless of wake-word
  state" requirement structurally.
- **Fish Speech not benchmarked** — see Benchmarks above.
- **Wake word is not wired into the realtime pipeline as a gate.** The
  `WakeWordProvider` interface and `OpenWakeWordProvider` adapter are
  implemented and unit-testable, but `voice/pipeline.py`'s realtime session
  doesn't yet use them to gate when STT starts listening — the pipeline is
  push-to-talk/continuous-session by design (the client decides when to
  open the WebSocket). Wiring wake-word detection as an always-on gate
  ahead of a real trained model existing seemed like the wrong order of
  operations; flagging it as intentionally deferred, not an oversight.
- **No live voice round-trip test** — see Tests above.
- **Memory search is not wired into assistant grounding.** Phase C built a
  complete, working retrieval service (FTS5 + vault + API), but Phase B's
  `AssistantRouter` only grounds on deterministic trading state, not on
  retrieved memory/vault context. Wiring "recall relevant past notes into
  the assistant's context" is a natural next step, deliberately not done
  here to avoid scope creep into a Phase B change under a Phase C commit.
- **`fastapi`/`starlette` were upgraded** from `0.115.0`/(implicit) to
  `0.141.1`/`1.6.0` mid-session — `pipecat-ai[websocket]` pulled in the
  newer versions as a transitive dependency, and I re-pinned
  `requirements.txt` to match after confirming the full test suite still
  passes at the new versions. Flagging this since it's outside the diff
  Phase A originally shipped.
- **Python 3.13 was used**, not the 3.12 named in `AGENTS.md`/`ARCHITECTURE.md`
  — this machine only had 3.13 and 3.7 available (`py -0` confirmed no 3.12
  install). Nothing in the codebase is 3.13-specific; it should run
  unmodified on 3.12 if that's later required, but this wasn't verified on
  3.12 in this session.

## Exact dependencies required from other agents

- **Antigravity (frontend)**: the WebSocket/HTTP contracts above are
  stable and ready to consume. For voice specifically:
  `/api/v1/voice/session` expects 16-bit PCM mono audio in and returns the
  same out (no WAV wrapping over the wire — `add_wav_header=False`); WAV
  wrapping is only used by the one-shot `/api/v1/voice/transcribe`
  (upload) and `/api/v1/voice/synthesize` (response) endpoints. Browser
  `MediaRecorder` typically produces webm/opus, not raw PCM — the frontend
  is responsible for any needed transcoding before hitting these
  endpoints; this backend does not transcode.
- **Codex (tests/tools)**: `apps/backend/README.md` documents how to run
  the test suite and what's expected to work with zero API keys (the
  `mock` provider path for assistant/STT/TTS/wake-word — confirmed via
  `tests/test_assistant_provider_fallback.py` and the voice mock tests).
  The `contracts/*.schema.json` validation lives in `app/contracts.py`,
  loading the canonical files directly (no forked copy) — useful if Codex
  wants to cross-check contract conformance independently.

## Next recommended action

1. A coordinator decides whether/when to merge `feature/v1-backend-voice`
   into `integration/v1`, per `AGENTS.md`'s merge-strategy note in
   `TASKS.md` (Cross-cutting / Post-Wave-1).
2. If someone has GPU hardware available, benchmark Fish Speech properly
   (run the local API server, compare against the Kokoro numbers above)
   before treating the current `TTS_PROVIDER=kokoro` default as final.
3. Training a real "TARS" openWakeWord model, if always-on wake-word
   listening (vs. push-to-talk) becomes a priority.
4. A live voice round-trip test once Antigravity has a client that can
   open `/api/v1/voice/session` with a real microphone.
5. Wiring memory search into assistant grounding (Known limitations above)
   if "recall past notes" becomes a desired assistant capability.
