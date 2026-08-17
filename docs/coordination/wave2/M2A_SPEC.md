# M2A_SPEC.md — Wave 2A: Windows-wide Assistant

Status: Phase 0 shared base. This is the milestone spec for Wave 2A (M2A);
interface detail lives in [M2A_INTERFACES.md](M2A_INTERFACES.md). V1 product
scope stays in [`../MASTER_SPEC.md`](../MASTER_SPEC.md) and is unchanged by
this milestone.

## Objective

Turn TARS from a companion application (open, foregrounded, V1 scope) into
an always-available Windows-wide assistant: running in the background,
summoned by a global hotkey from any application, aware of the foreground
window, and able to safely act on the user's Windows session (launch/focus
apps, browse files, open URLs, run vetted terminal commands, search
Obsidian) through a deterministic, auditable permission system — without an
LLM ever being able to bypass that permission system.

## Base

Branched from the certified V1 integration SHA
`2db4bac1538da8cb0fbaa68e3c21b90eb98ff0c6` (`integration/v1`), on branch
`integration/wave2`. `integration/v1` itself is untouched by Wave 2 work.

## Ownership (extends `AGENTS.md`'s table for Wave 2A)

| Agent | New/extended ownership | Branch | Worktree |
|---|---|---|---|
| **Claude Code** | `apps/backend/skills/` — Windows/core skill execution layer (windows app, filesystem, browser, terminal, Obsidian, voice-pipeline bridge) | `feature/wave2-core-skills` | `C:\TARS-Wave2-Claude` |
| **Codex** | `apps/backend/actions/` — action runtime: request intake, permission engine (deterministic RiskLevel enforcement), audit trail, skill registry/dispatch | `feature/wave2-action-runtime` | `C:\TARS-Wave2-Codex` |
| **Antigravity** | `apps/web/src-tauri/` (extended) + new HUD surface in `apps/web/` — background/tray lifecycle, autostart, global hotkey, active-window bridge (native side), HUD summon/confirm/result UI | `feature/wave2-native-shell` | `C:\TARS-Wave2-Antigravity` |
| **Integration coordinator** | Merges the three streams without squashing, runs the one milestone validation gate | `feature/wave2-m2a-integration` | `C:\TARS-Wave2-Integration` |

Shared/read-only during parallel Wave 2A implementation, same rule as
`AGENTS.md`: `contracts/*.schema.json`, `apps/backend/app/action_contracts.py`,
`apps/backend/app/contracts.py`'s action-* validators, and this doc pair.
A stream that needs one of these changed records the need in its own
handoff (`docs/coordination/handoffs/<agent>.md`) rather than editing it
directly — same escalation path as V1.

## Non-negotiables (carried over / extended from `AGENTS.md`)

- No trading execution, ever; Wave 2A does not touch the trading-event or
  assistant-message contracts.
- TARS must not duplicate `quant_brain`'s research/backtesting/validation
  logic (acceptance criterion 13) — nothing in Wave 2A grows toward that.
- Deterministic commands bypass the LLM when possible (acceptance
  criterion 12) — a skill's `classify_risk()`/`validate()`/`execute()` are
  plain code, not model calls; the LLM's role is limited to producing an
  `ActionRequest` when NL input requires interpretation, never to deciding
  whether that request is permitted.
- The permission system is deterministic and the LLM cannot bypass it
  (acceptance criterion 9) — enforcement lives in the action runtime
  (Codex), not in a skill and not in a prompt.
- No fabricated results: an `ActionResult` must reflect what actually
  happened (see `ActionResult.summary`/`status` docstrings in
  `action_contracts.py`) — never a "SUCCEEDED" status for something that
  wasn't actually performed, never a claimed wake-word verification that
  wasn't genuinely tested (acceptance criterion 10).
- Existing V1 behavior, tests, and contracts are preserved. Wave 2A is
  additive; nothing in `apps/backend/app/`, `apps/backend/{events,voice,
  assistant,memory}/`, or `apps/web/src/` (V1 surfaces) is weakened to make
  room for it.

## M2A acceptance criteria

1. **Background persistence.** TARS keeps running when its main window is
   closed/hidden; no user-visible "app is gone" state short of an explicit
   quit.
2. **Tray controls.** A system tray icon offers show/hide (HUD) and quit,
   with real effect, not placeholders.
3. **Optional autostart.** A Windows-login autostart toggle, off by
   default, using a legitimate Windows autostart mechanism (registry Run
   key or a Tauri-native equivalent) — not a scheduled task run as
   elevated/system unless the user explicitly opts into that distinction.
4. **Global hotkey.** A configurable global hotkey summons/hides the HUD
   while a different application has focus.
5. **Active-window context.** Native context capture: foreground
   executable/process, window title, window bounds. No screenshot or
   window-content capture in Wave 2A — that is explicitly out of scope
   (see `ActiveWindowContext` in `action_contracts.py`).
6. **Windows skills**: launch application, focus an existing running
   application, open a URL/default browser, open a file or folder,
   search/list files safely (read-only enumeration, no implicit deletes).
7. **Terminal skill.** The exact command is shown to the user before
   execution; state-changing commands require explicit confirmation;
   destructive/elevated/system-critical operations are blocked by default,
   not merely warned about.
8. **Obsidian.** Reuses the existing `apps/backend/memory/` vault
   indexing/search architecture (`MemoryService`, FTS5) for read/search —
   Wave 2A does not build a second memory system.
9. **Permission system.** Deterministic `RiskLevel` enforcement in the
   action runtime; an LLM has no code path to execute an action without
   going through it; every requested/executed/denied action is audited.
10. **Voice.** The existing local voice pipeline's global PTT/hotkey path
    feeds the same `ActionRequest` pipeline as HUD/hotkey-issued requests.
    The wake-word service may be enabled if genuinely functioning; if the
    custom "TARS" wake word has not been genuinely tested end-to-end, it
    must be marked `UNVERIFIED` in the handoff/report, never claimed
    working on the basis of code existing.
11. **HUD.** Summonable from anywhere; displays active context; displays
    the requested action; shows a confirmation UI for
    `CONFIRM_REQUIRED`-classified actions; displays the actual
    `ActionResult`, not an optimistic placeholder.
12. **Deterministic bypass.** Deterministic commands (e.g. "focus
    Notepad", a recognized fixed phrase) resolve to an `ActionRequest`
    without an LLM call, mirroring the V1
    `AssistantRouter`/deterministic-routing pattern in
    `apps/backend/assistant/router.py`.
13. **No `quant_brain` duplication.** Nothing in Wave 2A implements
    backtesting, cost modelling, walk-forward validation, DSR/statistical
    validation, or a strategy research database.

## Explicitly out of scope for M2A

- Screenshot or window-content capture of any kind.
- A custom-trained "TARS" wake-word model being *certified* working
  (openWakeWord path may exist per V1's known limitation — see
  `docs/coordination/handoffs/claude.md` — but stays `UNVERIFIED` unless
  someone actually proves it this milestone).
- Any elevation/UAC-bypass mechanism. Elevated/system-critical commands
  are blocked, not routed around.
- `quant_brain` integration of any kind (permanently out of Wave 2A too,
  not just deferred like V1).

## Validation cadence for this milestone

Implementation-first, targeted tests per change, one full validation gate
at integration (Phase 4) — not repeated full-suite runs per stream. See
each stream's own handoff file for what it validated locally before
pushing.
