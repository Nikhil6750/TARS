import { describe, it, expect } from 'vitest';
import {
  validateActionRequest,
  validateActionResult,
  validateActiveWindowContext,
} from '../contracts/action-validator';
import { ActionRequest, ActionResult, ActiveWindowContext } from '../types/actions';

describe('Wave 2A Action Contracts Validator', () => {
  it('validates canonical ActionRequest schema', () => {
    const validReq: ActionRequest = {
      schema_version: '1.0.0',
      id: '550e8400-e29b-41d4-a716-446655440000',
      skill: 'windows_app',
      action: 'focus',
      arguments: { target: 'Notepad' },
      source: 'hud',
      active_context: {
        executable: 'notepad.exe',
        process_id: 1234,
        window_title: 'Untitled - Notepad',
        window_bounds: { x: 100, y: 100, width: 800, height: 600 },
        captured_at: new Date().toISOString(),
      },
      requested_at: new Date().toISOString(),
    };

    const res = validateActionRequest(validReq);
    expect(res.success).toBe(true);
    if (res.success) {
      expect(res.data.skill).toBe('windows_app');
      expect(res.data.action).toBe('focus');
    }
  });

  it('rejects ActionRequest with forbidden extra properties (additionalProperties: false)', () => {
    const invalidReq = {
      schema_version: '1.0.0',
      id: '550e8400-e29b-41d4-a716-446655440000',
      skill: 'terminal',
      action: 'run_command',
      arguments: { command: 'dir' },
      source: 'hud',
      requested_at: new Date().toISOString(),
      forbidden_extra: 'invalid',
    };

    const res = validateActionRequest(invalidReq);
    expect(res.success).toBe(false);
    if (!res.success) {
      expect(res.errors.some((e) => e.includes('Forbidden property in ActionRequest'))).toBe(true);
    }
  });

  it('rejects ActionRequest with wrong schema_version or invalid source', () => {
    const badVersion = {
      schema_version: '2.0.0',
      id: '550e8400-e29b-41d4-a716-446655440000',
      skill: 'browser',
      action: 'open_url',
      arguments: {},
      source: 'invalid_source',
      requested_at: new Date().toISOString(),
    };

    const res = validateActionRequest(badVersion);
    expect(res.success).toBe(false);
    if (!res.success) {
      expect(res.errors.some((e) => e.includes('schema_version'))).toBe(true);
      expect(res.errors.some((e) => e.includes('source'))).toBe(true);
    }
  });

  it('validates canonical ActionResult schema for terminal and non-terminal statuses', () => {
    const succeededResult: ActionResult = {
      schema_version: '1.0.0',
      request_id: '550e8400-e29b-41d4-a716-446655440000',
      status: 'SUCCEEDED',
      risk_level: 'LOW_RISK',
      summary: 'Focused Notepad application',
      data: { pid: 1234 },
      error: null,
      started_at: new Date().toISOString(),
      completed_at: new Date().toISOString(),
    };

    const res = validateActionResult(succeededResult);
    expect(res.success).toBe(true);

    const pendingResult: ActionResult = {
      schema_version: '1.0.0',
      request_id: '550e8400-e29b-41d4-a716-446655440000',
      status: 'CONFIRMATION_REQUIRED',
      risk_level: 'CONFIRM_REQUIRED',
      summary: 'Requires confirmation to execute command: rm file.txt',
      data: { command: 'rm file.txt' },
      error: null,
      started_at: new Date().toISOString(),
      completed_at: null,
    };

    const pendingRes = validateActionResult(pendingResult);
    expect(pendingRes.success).toBe(true);
  });

  it('validates active window context schema and bounds', () => {
    const ctx: ActiveWindowContext = {
      executable: 'code.exe',
      process_id: 9988,
      window_title: 'TARS - Visual Studio Code',
      window_bounds: { x: 0, y: 0, width: 1920, height: 1080 },
      captured_at: new Date().toISOString(),
    };

    const res = validateActiveWindowContext(ctx);
    expect(res.success).toBe(true);

    const badCtx = {
      executable: '',
      window_title: 123, // should be string
    };
    const badRes = validateActiveWindowContext(badCtx);
    expect(badRes.success).toBe(false);
  });
});
