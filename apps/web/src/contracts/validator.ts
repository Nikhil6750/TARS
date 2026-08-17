/**
 * Strict Contract Validators for TARS Events
 * Directly enforces contracts/trading-event.schema.json & contracts/assistant-message.schema.json
 */

import { TARSTradingEvent, TradingEventState, TradingEventSource, TradeDirection, ValidationStatus } from '../types/trading-event';
import { TARSAssistantMessage, AssistantRole, InputMode } from '../types/assistant-message';

export type ValidationResult<T> =
  | { success: true; data: T }
  | { success: false; errors: string[] };

const ALLOWED_TRADING_EVENT_KEYS = new Set([
  'schema_version',
  'event_id',
  'timestamp',
  'source',
  'symbol',
  'strategy_id',
  'state',
  'direction',
  'entry',
  'stop_loss',
  'take_profit',
  'risk_reward',
  'risk_percent',
  'validation_status',
  'reason_codes',
  'warnings',
  'expires_at'
]);

const ALLOWED_SOURCES: Set<TradingEventSource> = new Set(['mock', 'quant_brain', 'manual']);

const ALLOWED_STATES: Set<TradingEventState> = new Set([
  'IDLE',
  'SETUP_DEVELOPING',
  'SETUP_VALID',
  'SETUP_INVALIDATED',
  'RISK_WARNING',
  'SYSTEM_WARNING'
]);

const ALLOWED_DIRECTIONS: Set<TradeDirection> = new Set(['LONG', 'SHORT', 'NONE', null]);

const ALLOWED_VALIDATION_STATUSES: Set<ValidationStatus> = new Set([
  'PENDING',
  'VALID',
  'INVALID',
  'EXPIRED'
]);

/**
 * Validates a trading event against contracts/trading-event.schema.json (v1.0.0)
 * Rejects unknown fields (additionalProperties: false) and schema version mismatches.
 */
export function validateTradingEvent(raw: unknown): ValidationResult<TARSTradingEvent> {
  const errors: string[] = [];

  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    return { success: false, errors: ['Payload must be a non-null object'] };
  }

  const obj = raw as Record<string, unknown>;

  // Check additionalProperties: false
  for (const key of Object.keys(obj)) {
    if (!ALLOWED_TRADING_EVENT_KEYS.has(key)) {
      errors.push(`Forbidden additional property detected: "${key}"`);
    }
  }

  // schema_version
  if (obj.schema_version !== '1.0.0') {
    errors.push(`schema_version must be exactly "1.0.0", received "${String(obj.schema_version)}"`);
  }

  // event_id
  if (typeof obj.event_id !== 'string' || obj.event_id.trim() === '') {
    errors.push('event_id is required and must be a valid non-empty string');
  }

  // timestamp
  if (typeof obj.timestamp !== 'string' || Number.isNaN(Date.parse(obj.timestamp))) {
    errors.push('timestamp is required and must be a valid ISO 8601 date string');
  }

  // source
  if (!ALLOWED_SOURCES.has(obj.source as TradingEventSource)) {
    errors.push(`source must be one of ["mock", "quant_brain", "manual"], received "${String(obj.source)}"`);
  }

  // symbol
  if (typeof obj.symbol !== 'string' || obj.symbol.length < 1) {
    errors.push('symbol is required and must be a non-empty string');
  }

  // strategy_id (optional, string | null)
  if (obj.strategy_id !== undefined && obj.strategy_id !== null && typeof obj.strategy_id !== 'string') {
    errors.push('strategy_id must be a string, null, or omitted');
  }

  // state
  if (!ALLOWED_STATES.has(obj.state as TradingEventState)) {
    errors.push(`state must be one of ["IDLE", "SETUP_DEVELOPING", "SETUP_VALID", "SETUP_INVALIDATED", "RISK_WARNING", "SYSTEM_WARNING"], received "${String(obj.state)}"`);
  }

  // direction (optional, LONG | SHORT | NONE | null)
  if (obj.direction !== undefined && !ALLOWED_DIRECTIONS.has(obj.direction as TradeDirection)) {
    errors.push(`direction must be "LONG", "SHORT", "NONE", or null, received "${String(obj.direction)}"`);
  }

  // Numeric fields (optional, number | null)
  for (const numField of ['entry', 'stop_loss', 'take_profit', 'risk_reward'] as const) {
    if (obj[numField] !== undefined && obj[numField] !== null && typeof obj[numField] !== 'number') {
      errors.push(`${numField} must be a number or null`);
    }
  }

  // risk_percent (minimum 0)
  if (obj.risk_percent !== undefined && obj.risk_percent !== null) {
    if (typeof obj.risk_percent !== 'number' || obj.risk_percent < 0) {
      errors.push('risk_percent must be a non-negative number or null');
    }
  }

  // validation_status
  if (!ALLOWED_VALIDATION_STATUSES.has(obj.validation_status as ValidationStatus)) {
    errors.push(`validation_status must be one of ["PENDING", "VALID", "INVALID", "EXPIRED"], received "${String(obj.validation_status)}"`);
  }

  // reason_codes (array of strings)
  if (obj.reason_codes !== undefined && obj.reason_codes !== null) {
    if (!Array.isArray(obj.reason_codes) || !obj.reason_codes.every((item) => typeof item === 'string')) {
      errors.push('reason_codes must be an array of strings');
    }
  }

  // warnings (array of strings)
  if (obj.warnings !== undefined && obj.warnings !== null) {
    if (!Array.isArray(obj.warnings) || !obj.warnings.every((item) => typeof item === 'string')) {
      errors.push('warnings must be an array of strings');
    }
  }

  // expires_at (optional ISO string | null)
  if (obj.expires_at !== undefined && obj.expires_at !== null) {
    if (typeof obj.expires_at !== 'string' || Number.isNaN(Date.parse(obj.expires_at))) {
      errors.push('expires_at must be an ISO 8601 date string or null');
    }
  }

  if (errors.length > 0) {
    return { success: false, errors };
  }

  return { success: true, data: raw as TARSTradingEvent };
}

const ALLOWED_ASSISTANT_MESSAGE_KEYS = new Set([
  'schema_version',
  'message_id',
  'conversation_id',
  'timestamp',
  'role',
  'content',
  'input_mode',
  'audio_ref',
  'related_event_id',
  'intent',
  'providers',
  'error'
]);

const ALLOWED_ROLES: Set<AssistantRole> = new Set(['user', 'assistant', 'system']);
const ALLOWED_INPUT_MODES: Set<InputMode> = new Set(['text', 'voice']);

/**
 * Validates an assistant message against contracts/assistant-message.schema.json (v1.0.0)
 */
export function validateAssistantMessage(raw: unknown): ValidationResult<TARSAssistantMessage> {
  const errors: string[] = [];

  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    return { success: false, errors: ['Payload must be a non-null object'] };
  }

  const obj = raw as Record<string, unknown>;

  // Check additionalProperties: false
  for (const key of Object.keys(obj)) {
    if (!ALLOWED_ASSISTANT_MESSAGE_KEYS.has(key)) {
      errors.push(`Forbidden additional property detected in assistant message: "${key}"`);
    }
  }

  // schema_version
  if (obj.schema_version !== '1.0.0') {
    errors.push(`schema_version must be exactly "1.0.0", received "${String(obj.schema_version)}"`);
  }

  // message_id
  if (typeof obj.message_id !== 'string' || obj.message_id.trim() === '') {
    errors.push('message_id is required and must be a non-empty string');
  }

  // conversation_id
  if (typeof obj.conversation_id !== 'string' || obj.conversation_id.trim() === '') {
    errors.push('conversation_id is required and must be a non-empty string');
  }

  // timestamp
  if (typeof obj.timestamp !== 'string' || Number.isNaN(Date.parse(obj.timestamp))) {
    errors.push('timestamp is required and must be a valid ISO 8601 date string');
  }

  // role
  if (!ALLOWED_ROLES.has(obj.role as AssistantRole)) {
    errors.push(`role must be one of ["user", "assistant", "system"], received "${String(obj.role)}"`);
  }

  // content
  if (typeof obj.content !== 'string') {
    errors.push('content is required and must be a string');
  }

  // input_mode
  if (!ALLOWED_INPUT_MODES.has(obj.input_mode as InputMode)) {
    errors.push(`input_mode must be "text" or "voice", received "${String(obj.input_mode)}"`);
  }

  // providers (optional object)
  if (obj.providers !== undefined && obj.providers !== null) {
    if (typeof obj.providers !== 'object' || Array.isArray(obj.providers)) {
      errors.push('providers must be an object');
    } else {
      const p = obj.providers as Record<string, unknown>;
      const allowedProviderKeys = new Set(['stt', 'assistant', 'tts']);
      for (const k of Object.keys(p)) {
        if (!allowedProviderKeys.has(k)) {
          errors.push(`Forbidden provider key: "${k}"`);
        }
      }
    }
  }

  if (errors.length > 0) {
    return { success: false, errors };
  }

  return { success: true, data: raw as TARSAssistantMessage };
}
