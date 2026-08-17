/**
 * Strict Contract Validators for Wave 2A Action Requests and Action Results
 * Directly enforces contracts/action-request.schema.json and contracts/action-result.schema.json
 */

import {
  ActionRequest,
  ActionResult,
  ActionSource,
  ActionStatus,
  RiskLevel,
  ActiveWindowContext,
} from '../types/actions';
import { ValidationResult } from './validator';

const ALLOWED_ACTION_REQUEST_KEYS = new Set([
  'schema_version',
  'id',
  'skill',
  'action',
  'arguments',
  'source',
  'active_context',
  'requested_at',
]);

const ALLOWED_ACTION_SOURCES: Set<ActionSource> = new Set([
  'hud',
  'voice_ptt',
  'voice_wake_word',
  'hotkey',
  'deterministic',
  'api',
]);

const ALLOWED_ACTIVE_CONTEXT_KEYS = new Set([
  'executable',
  'process_id',
  'window_title',
  'window_bounds',
  'captured_at',
]);

const ALLOWED_BOUNDS_KEYS = new Set(['x', 'y', 'width', 'height']);

export function validateActiveWindowContext(raw: unknown): ValidationResult<ActiveWindowContext> {
  const errors: string[] = [];

  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    return { success: false, errors: ['active_context must be a non-null object'] };
  }

  const obj = raw as Record<string, unknown>;

  for (const key of Object.keys(obj)) {
    if (!ALLOWED_ACTIVE_CONTEXT_KEYS.has(key)) {
      errors.push(`Forbidden property in active_context: "${key}"`);
    }
  }

  if (typeof obj.executable !== 'string' || obj.executable.trim() === '') {
    errors.push('active_context.executable is required and must be a non-empty string');
  }

  if (typeof obj.window_title !== 'string') {
    errors.push('active_context.window_title is required and must be a string');
  }

  if (obj.process_id !== undefined && obj.process_id !== null && typeof obj.process_id !== 'number') {
    errors.push('active_context.process_id must be an integer or null');
  }

  if (obj.window_bounds !== undefined && obj.window_bounds !== null) {
    if (typeof obj.window_bounds !== 'object' || Array.isArray(obj.window_bounds)) {
      errors.push('active_context.window_bounds must be an object or null');
    } else {
      const bounds = obj.window_bounds as Record<string, unknown>;
      for (const k of Object.keys(bounds)) {
        if (!ALLOWED_BOUNDS_KEYS.has(k)) {
          errors.push(`Forbidden property in window_bounds: "${k}"`);
        }
      }
      for (const req of ['x', 'y', 'width', 'height'] as const) {
        if (typeof bounds[req] !== 'number') {
          errors.push(`window_bounds.${req} is required and must be a number`);
        }
      }
    }
  }

  if (obj.captured_at !== undefined && obj.captured_at !== null) {
    if (typeof obj.captured_at !== 'string' || Number.isNaN(Date.parse(obj.captured_at))) {
      errors.push('active_context.captured_at must be an ISO 8601 date string or null');
    }
  }

  if (errors.length > 0) {
    return { success: false, errors };
  }

  return { success: true, data: raw as ActiveWindowContext };
}

export function validateActionRequest(raw: unknown): ValidationResult<ActionRequest> {
  const errors: string[] = [];

  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    return { success: false, errors: ['ActionRequest payload must be a non-null object'] };
  }

  const obj = raw as Record<string, unknown>;

  for (const key of Object.keys(obj)) {
    if (!ALLOWED_ACTION_REQUEST_KEYS.has(key)) {
      errors.push(`Forbidden property in ActionRequest: "${key}"`);
    }
  }

  if (obj.schema_version !== '1.0.0') {
    errors.push(`schema_version must be exactly "1.0.0", received "${String(obj.schema_version)}"`);
  }

  if (typeof obj.id !== 'string' || obj.id.trim() === '') {
    errors.push('id is required and must be a valid non-empty UUID string');
  }

  if (typeof obj.skill !== 'string' || obj.skill.trim() === '') {
    errors.push('skill is required and must be a non-empty string');
  }

  if (typeof obj.action !== 'string' || obj.action.trim() === '') {
    errors.push('action is required and must be a non-empty string');
  }

  if (typeof obj.arguments !== 'object' || obj.arguments === null || Array.isArray(obj.arguments)) {
    errors.push('arguments must be a non-null object');
  }

  if (!ALLOWED_ACTION_SOURCES.has(obj.source as ActionSource)) {
    errors.push(`source must be one of ["hud", "voice_ptt", "voice_wake_word", "hotkey", "deterministic", "api"], received "${String(obj.source)}"`);
  }

  if (typeof obj.requested_at !== 'string' || Number.isNaN(Date.parse(obj.requested_at))) {
    errors.push('requested_at is required and must be a valid ISO 8601 date string');
  }

  if (obj.active_context !== undefined && obj.active_context !== null) {
    const ctxVal = validateActiveWindowContext(obj.active_context);
    if (!ctxVal.success) {
      errors.push(...ctxVal.errors);
    }
  }

  if (errors.length > 0) {
    return { success: false, errors };
  }

  return { success: true, data: raw as ActionRequest };
}

const ALLOWED_ACTION_RESULT_KEYS = new Set([
  'schema_version',
  'request_id',
  'status',
  'risk_level',
  'summary',
  'data',
  'error',
  'started_at',
  'completed_at',
]);

const ALLOWED_ACTION_STATUSES: Set<ActionStatus> = new Set([
  'PENDING',
  'CONFIRMATION_REQUIRED',
  'DENIED',
  'BLOCKED',
  'RUNNING',
  'SUCCEEDED',
  'FAILED',
]);

const ALLOWED_RISK_LEVELS: Set<RiskLevel> = new Set([
  'READ_ONLY',
  'LOW_RISK',
  'CONFIRM_REQUIRED',
  'BLOCKED',
]);

export function validateActionResult(raw: unknown): ValidationResult<ActionResult> {
  const errors: string[] = [];

  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    return { success: false, errors: ['ActionResult payload must be a non-null object'] };
  }

  const obj = raw as Record<string, unknown>;

  for (const key of Object.keys(obj)) {
    if (!ALLOWED_ACTION_RESULT_KEYS.has(key)) {
      errors.push(`Forbidden property in ActionResult: "${key}"`);
    }
  }

  if (obj.schema_version !== '1.0.0') {
    errors.push(`schema_version must be exactly "1.0.0", received "${String(obj.schema_version)}"`);
  }

  if (typeof obj.request_id !== 'string' || obj.request_id.trim() === '') {
    errors.push('request_id is required and must be a valid non-empty UUID string');
  }

  if (!ALLOWED_ACTION_STATUSES.has(obj.status as ActionStatus)) {
    errors.push(`status must be one of ["PENDING", "CONFIRMATION_REQUIRED", "DENIED", "BLOCKED", "RUNNING", "SUCCEEDED", "FAILED"], received "${String(obj.status)}"`);
  }

  if (obj.risk_level !== undefined && obj.risk_level !== null) {
    if (!ALLOWED_RISK_LEVELS.has(obj.risk_level as RiskLevel)) {
      errors.push(`risk_level must be one of ["READ_ONLY", "LOW_RISK", "CONFIRM_REQUIRED", "BLOCKED"], received "${String(obj.risk_level)}"`);
    }
  }

  if (typeof obj.summary !== 'string') {
    errors.push('summary is required and must be a string');
  }

  if (typeof obj.data !== 'object' || obj.data === null || Array.isArray(obj.data)) {
    errors.push('data must be a non-null object');
  }

  if (obj.error !== undefined && obj.error !== null && typeof obj.error !== 'string') {
    errors.push('error must be a string or null');
  }

  if (typeof obj.started_at !== 'string' || Number.isNaN(Date.parse(obj.started_at))) {
    errors.push('started_at is required and must be a valid ISO 8601 date string');
  }

  if (obj.completed_at !== undefined && obj.completed_at !== null) {
    if (typeof obj.completed_at !== 'string' || Number.isNaN(Date.parse(obj.completed_at))) {
      errors.push('completed_at must be an ISO 8601 date string or null');
    }
  }

  if (errors.length > 0) {
    return { success: false, errors };
  }

  return { success: true, data: raw as ActionResult };
}
