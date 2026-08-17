import { describe, it, expect, vi, beforeEach } from 'vitest';
import { ActionRuntimeClient } from '../services/actions';

describe('Wave 2A ActionRuntimeClient', () => {
  let client: ActionRuntimeClient;

  beforeEach(() => {
    client = new ActionRuntimeClient('http://127.0.0.1:59999');
    vi.restoreAllMocks();
  });

  it('creates well-formed ActionRequest adhering to schema', () => {
    const req = client.createRequest({
      skill: 'windows_app',
      action: 'launch',
      arguments: { target: 'calc.exe' },
      source: 'hud',
    });

    expect(req.schema_version).toBe('1.0.0');
    expect(req.skill).toBe('windows_app');
    expect(req.action).toBe('launch');
    expect(req.arguments.target).toBe('calc.exe');
    expect(req.source).toBe('hud');
    expect(typeof req.id).toBe('string');
  });

  describe('Deterministic Command Interpreter (M2A Criterion 12)', () => {
    it('parses "focus Notepad" into deterministic windows_app.focus ActionRequest', () => {
      const req = client.parseDeterministicCommand('focus Notepad');
      expect(req).not.toBeNull();
      expect(req?.skill).toBe('windows_app');
      expect(req?.action).toBe('focus');
      expect(req?.arguments.target).toBe('Notepad');
      expect(req?.source).toBe('deterministic');
    });

    it('parses "launch Calculator" into deterministic windows_app.launch ActionRequest', () => {
      const req = client.parseDeterministicCommand('launch Calculator');
      expect(req).not.toBeNull();
      expect(req?.skill).toBe('windows_app');
      expect(req?.action).toBe('launch');
      expect(req?.arguments.target).toBe('Calculator');
    });

    it('parses bare "open notepad" (no "app"/"application" keyword) into windows_app.launch', () => {
      // Regression: the bare-form launch pattern originally accepted only
      // "launch X" / "start X", not "open X" -- so the single most natural
      // phrasing for launching an app fell through to the LLM/chat path
      // instead of the deterministic bypass.
      const req = client.parseDeterministicCommand('open notepad');
      expect(req).not.toBeNull();
      expect(req?.skill).toBe('windows_app');
      expect(req?.action).toBe('launch');
      expect(req?.arguments.target).toBe('notepad');
    });

    it('strips trailing STT punctuation so "Open Notepad." still resolves a launchable target', () => {
      // Regression: verified against real faster-whisper output for spoken
      // "open notepad" -- it comes back as "Open Notepad." (trailing
      // period). Left unstripped, the captured target "Notepad." never
      // resolves via shutil.which on the backend (only "Notepad" does).
      const req = client.parseDeterministicCommand('Open Notepad.');
      expect(req).not.toBeNull();
      expect(req?.skill).toBe('windows_app');
      expect(req?.action).toBe('launch');
      expect(req?.arguments.target).toBe('Notepad');
    });

    it('parses "open url https://tradingview.com" into deterministic browser.open_url ActionRequest', () => {
      const req = client.parseDeterministicCommand('open url https://tradingview.com');
      expect(req).not.toBeNull();
      expect(req?.skill).toBe('browser');
      expect(req?.action).toBe('open_url');
      expect(req?.arguments.url).toBe('https://tradingview.com');
    });

    it('parses "search files config.json" into deterministic filesystem.search ActionRequest', () => {
      const req = client.parseDeterministicCommand('search files config.json');
      expect(req).not.toBeNull();
      expect(req?.skill).toBe('filesystem');
      expect(req?.action).toBe('search');
      expect(req?.arguments.query).toBe('config.json');
    });

    it('parses "search obsidian risk parameters" into deterministic obsidian.search ActionRequest', () => {
      const req = client.parseDeterministicCommand('search obsidian risk parameters');
      expect(req).not.toBeNull();
      expect(req?.skill).toBe('obsidian');
      expect(req?.action).toBe('search');
      expect(req?.arguments.query).toBe('risk parameters');
    });

    it('parses "terminal: Get-Process" into deterministic terminal.run_command ActionRequest', () => {
      const req = client.parseDeterministicCommand('terminal: Get-Process');
      expect(req).not.toBeNull();
      expect(req?.skill).toBe('terminal');
      expect(req?.action).toBe('run_command');
      expect(req?.arguments.command).toBe('Get-Process');
    });

    it('returns null for non-deterministic natural language input', () => {
      const req = client.parseDeterministicCommand('What is the current market sentiment on Gold?');
      expect(req).toBeNull();
    });
  });

  describe('Risk Level Classification & Confirmation Engine', () => {
    it('correctly classifies terminal commands as CONFIRM_REQUIRED and requires user response', async () => {
      const req = client.createRequest({
        skill: 'terminal',
        action: 'run_command',
        arguments: { command: 'git status' },
      });

      const initialResult = await client.submitAction(req);
      expect(initialResult.status).toBe('CONFIRMATION_REQUIRED');
      expect(initialResult.risk_level).toBe('CONFIRM_REQUIRED');
      expect(initialResult.completed_at).toBeNull();

      // User confirms execution
      const confirmedResult = await client.respondToConfirmation(req.id, 'test-token', true);
      expect(confirmedResult.status).toBe('SUCCEEDED');
      expect(confirmedResult.risk_level).toBe('CONFIRM_REQUIRED');
      expect(confirmedResult.completed_at).not.toBeNull();
    });

    it('handles explicit user denial of confirmation', async () => {
      const req = client.createRequest({
        skill: 'terminal',
        action: 'run_command',
        arguments: { command: 'npm install' },
      });

      await client.submitAction(req);

      const deniedResult = await client.respondToConfirmation(req.id, 'test-token', false, 'Command not authorized by user');
      expect(deniedResult.status).toBe('DENIED');
      expect(deniedResult.error).toContain('not authorized');
      expect(deniedResult.completed_at).not.toBeNull();
    });

    it('permanently blocks destructive system commands (RiskLevel.BLOCKED)', async () => {
      const req = client.createRequest({
        skill: 'terminal',
        action: 'run_command',
        arguments: { command: 'format C: /y' },
      });

      const blockedResult = await client.submitAction(req);
      expect(blockedResult.status).toBe('BLOCKED');
      expect(blockedResult.risk_level).toBe('BLOCKED');
      expect(blockedResult.error).toContain('permanently blocked');
    });
  });
});
