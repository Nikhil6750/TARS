/**
 * Mock Trading Event & Assistant Message Generator
 * Generates schema-valid events strictly adhering to contracts/trading-event.schema.json (v1.0.0)
 * and contracts/assistant-message.schema.json (v1.0.0).
 */

import { TARSTradingEvent, TradingEventState } from '../types/trading-event';
import { TARSAssistantMessage } from '../types/assistant-message';

function generateUUID(): string {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === 'x' ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

const SAMPLE_SYMBOLS = ['XAUUSD', 'NQ', 'ES', 'EURUSD', 'BTCUSD', 'USO'];

const SAMPLE_SETUPS: Array<Partial<TARSTradingEvent>> = [
  {
    symbol: 'XAUUSD',
    strategy_id: 'strat_orderblock_fvg_v2',
    state: 'SETUP_VALID',
    direction: 'LONG',
    entry: 2684.50,
    stop_loss: 2676.00,
    take_profit: 2708.50,
    risk_reward: 2.82,
    risk_percent: 1.0,
    validation_status: 'VALID',
    reason_codes: ['H4_ORDER_BLOCK_TAP', 'M15_FVG_CONFLUENCE', 'LIQUIDITY_SWEEP_CONFIRMED'],
    warnings: ['High Impact USD CPI data in 45m', 'DXY approaching 104.50 resistance']
  },
  {
    symbol: 'NQ',
    strategy_id: 'strat_opening_range_breakout',
    state: 'SETUP_DEVELOPING',
    direction: 'SHORT',
    entry: 20420.25,
    stop_loss: 20475.00,
    take_profit: 20265.00,
    risk_reward: 2.83,
    risk_percent: 0.75,
    validation_status: 'PENDING',
    reason_codes: ['OVERNIGHT_HIGH_REJECTION', 'VWAP_NEGATIVE_SLOPE'],
    warnings: ['Awaiting 09:30 EST regular market open liquidity flush']
  },
  {
    symbol: 'BTCUSD',
    strategy_id: 'strat_mean_reversion_bollinger',
    state: 'SETUP_INVALIDATED',
    direction: 'LONG',
    entry: 96400.00,
    stop_loss: 95800.00,
    take_profit: 98100.00,
    risk_reward: 2.83,
    risk_percent: 0.5,
    validation_status: 'INVALID',
    reason_codes: ['STRUCTURAL_BREAK_DOWN', 'VOLUME_ANOMALY_BEARISH'],
    warnings: ['Invalidated due to high-volume breakdown through key support level']
  },
  {
    symbol: 'ES',
    strategy_id: 'strat_trend_continuation_ema',
    state: 'RISK_WARNING',
    direction: 'LONG',
    entry: 5880.50,
    stop_loss: 5865.00,
    take_profit: 5925.00,
    risk_reward: 2.87,
    risk_percent: 1.5,
    validation_status: 'VALID',
    reason_codes: ['ACCOUNT_CORRELATION_THRESHOLD_EXCEEDED'],
    warnings: ['Aggregate S&P + Nasdaq long exposure exceeds 2.25% account max risk threshold']
  },
  {
    symbol: 'EURUSD',
    strategy_id: null,
    state: 'SYSTEM_WARNING',
    direction: 'NONE',
    entry: null,
    stop_loss: null,
    take_profit: null,
    risk_reward: null,
    risk_percent: null,
    validation_status: 'PENDING',
    reason_codes: ['ECB_PRESS_CONFERENCE_BLACKOUT'],
    warnings: ['ECB Rate Decision press conference in progress — market spread widening']
  },
  {
    symbol: 'USO',
    strategy_id: 'strat_gap_fill_v1',
    state: 'IDLE',
    direction: 'NONE',
    entry: null,
    stop_loss: null,
    take_profit: null,
    risk_reward: null,
    risk_percent: null,
    validation_status: 'PENDING',
    reason_codes: ['NO_ACTIVE_TRIGGER'],
    warnings: []
  }
];

export function createMockTradingEvent(custom?: Partial<TARSTradingEvent>): TARSTradingEvent {
  const index = Math.floor(Math.random() * SAMPLE_SETUPS.length);
  const template = SAMPLE_SETUPS[index];
  const now = new Date().toISOString();

  const event: TARSTradingEvent = {
    schema_version: '1.0.0',
    event_id: generateUUID(),
    timestamp: now,
    source: 'mock',
    symbol: template.symbol || SAMPLE_SYMBOLS[Math.floor(Math.random() * SAMPLE_SYMBOLS.length)],
    strategy_id: template.strategy_id,
    state: (template.state || 'IDLE') as TradingEventState,
    direction: template.direction,
    entry: template.entry,
    stop_loss: template.stop_loss,
    take_profit: template.take_profit,
    risk_reward: template.risk_reward,
    risk_percent: template.risk_percent,
    validation_status: template.validation_status || 'PENDING',
    reason_codes: template.reason_codes || [],
    warnings: template.warnings || [],
    expires_at: new Date(Date.now() + 3600 * 1000).toISOString(),
    ...custom
  };

  return event;
}

export function createMockAssistantReply(
  userQuery: string,
  conversationId: string,
  activeSetups: TARSTradingEvent[]
): TARSAssistantMessage {
  const query = userQuery.toLowerCase();
  let content = `Acknowledged. Monitoring ${activeSetups.length} active setups across instruments.`;
  let intent = 'general_status';
  let relatedEventId: string | null = null;

  if (query.includes('gold') || query.includes('xau')) {
    const gold = activeSetups.find((s) => s.symbol.includes('XAU'));
    if (gold) {
      content = `Gold (XAUUSD) has an active LONG setup (Strategy: ${gold.strategy_id}). Entry: ${gold.entry}, Stop Loss: ${gold.stop_loss}, Target: ${gold.take_profit} (R:R ${gold.risk_reward}). Caution: ${gold.warnings?.join('; ') || 'None'}.`;
      relatedEventId = gold.event_id;
      intent = 'symbol_inspection';
    } else {
      content = 'Gold (XAUUSD) is currently in IDLE state. No valid orderblock or FVG triggers detected on H4.';
    }
  } else if (query.includes('risk') || query.includes('warning') || query.includes('exposure')) {
    content = 'Current aggregate risk across active setups is 2.25%. ES & NQ co-directional exposure is elevated ahead of macroeconomic data release.';
    intent = 'risk_overview';
  } else if (query.includes('setup') || query.includes('trade') || query.includes('active')) {
    const valid = activeSetups.filter((s) => s.state === 'SETUP_VALID');
    content = `Currently tracking ${valid.length} validated setups: ${valid.map((s) => `${s.symbol} ${s.direction} @ ${s.entry}`).join(', ') || 'None'}.`;
    intent = 'setups_summary';
  } else if (query.includes('help') || query.includes('who are you')) {
    content = 'I am TARS, your quantitative trading companion. I monitor trade setups, calculate deterministic risk-to-reward ratios, surface structural invalidations, and provide zero-latency voice/text intelligence.';
    intent = 'identity_help';
  }

  return {
    schema_version: '1.0.0',
    message_id: generateUUID(),
    conversation_id: conversationId,
    timestamp: new Date().toISOString(),
    role: 'assistant',
    content,
    input_mode: 'text',
    audio_ref: null,
    related_event_id: relatedEventId,
    intent,
    providers: {
      stt: null,
      assistant: 'ClaudeCodeProvider',
      tts: 'FishSpeechLocal'
    },
    error: null
  };
}
