import { describe, it, expect } from 'vitest';
import { validateTradingEvent, validateAssistantMessage } from '../contracts/validator';
import { TARSTradingEvent } from '../types/trading-event';
import { TARSAssistantMessage } from '../types/assistant-message';

describe('Canonical Contract Validator', () => {
  const validTradingEvent: TARSTradingEvent = {
    schema_version: '1.0.0',
    event_id: 'a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d',
    timestamp: '2026-08-16T12:00:00Z',
    source: 'mock',
    symbol: 'XAUUSD',
    strategy_id: 'strat_orderblock_v2',
    state: 'SETUP_VALID',
    direction: 'LONG',
    entry: 2684.50,
    stop_loss: 2676.00,
    take_profit: 2708.50,
    risk_reward: 2.82,
    risk_percent: 1.0,
    validation_status: 'VALID',
    reason_codes: ['H4_DEMAND_TAP', 'M15_FVG'],
    warnings: ['High impact news in 1hr'],
    expires_at: '2026-08-16T14:00:00Z'
  };

  it('validates a correct canonical trading event', () => {
    const result = validateTradingEvent(validTradingEvent);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.symbol).toBe('XAUUSD');
      expect(result.data.risk_reward).toBe(2.82);
    }
  });

  it('rejects an event with schema_version mismatch', () => {
    const invalid = { ...validTradingEvent, schema_version: '2.0.0' };
    const result = validateTradingEvent(invalid);
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.errors.some((e) => e.includes('schema_version must be exactly "1.0.0"'))).toBe(true);
    }
  });

  it('rejects an event with forbidden additional properties (e.g. fake confidence)', () => {
    const invalid = { ...validTradingEvent, ai_confidence_percent: 94.5 };
    const result = validateTradingEvent(invalid);
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.errors.some((e) => e.includes('Forbidden additional property detected'))).toBe(true);
    }
  });

  it('rejects an event with invalid state or source enum', () => {
    const invalid = { ...validTradingEvent, state: 'NOT_A_REAL_STATE', source: 'unauthorized_source' };
    const result = validateTradingEvent(invalid);
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.errors.length).toBeGreaterThanOrEqual(2);
    }
  });

  const validAssistantMessage: TARSAssistantMessage = {
    schema_version: '1.0.0',
    message_id: 'm1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d',
    conversation_id: 'c1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d',
    timestamp: '2026-08-16T12:00:00Z',
    role: 'assistant',
    content: 'Gold setup valid at 2684.50. Risk is within 1% threshold.',
    input_mode: 'text',
    audio_ref: null,
    related_event_id: 'a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d',
    intent: 'setup_inquiry',
    providers: {
      stt: null,
      assistant: 'ClaudeCodeProvider',
      tts: 'FishSpeechLocal'
    },
    error: null
  };

  it('validates a correct canonical assistant message', () => {
    const result = validateAssistantMessage(validAssistantMessage);
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.role).toBe('assistant');
      expect(result.data.providers?.assistant).toBe('ClaudeCodeProvider');
    }
  });

  it('rejects an assistant message with invalid role or input_mode', () => {
    const invalid = { ...validAssistantMessage, role: 'invalid_role', input_mode: 'telepathy' };
    const result = validateAssistantMessage(invalid);
    expect(result.success).toBe(false);
  });
});
