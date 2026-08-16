/**
 * Canonical TARS Trading Event Types
 * Generated & mirrored from contracts/trading-event.schema.json (v1.0.0)
 * Note: Never add AI confidence or probability fields (ADR-004).
 */

export type TradingEventState =
  | 'IDLE'
  | 'SETUP_DEVELOPING'
  | 'SETUP_VALID'
  | 'SETUP_INVALIDATED'
  | 'RISK_WARNING'
  | 'SYSTEM_WARNING';

export type TradingEventSource = 'mock' | 'quant_brain' | 'manual';

export type TradeDirection = 'LONG' | 'SHORT' | 'NONE' | null;

export type ValidationStatus = 'PENDING' | 'VALID' | 'INVALID' | 'EXPIRED';

export interface TARSTradingEvent {
  schema_version: '1.0.0';
  event_id: string;
  timestamp: string;
  source: TradingEventSource;
  symbol: string;
  strategy_id?: string | null;
  state: TradingEventState;
  direction?: TradeDirection;
  entry?: number | null;
  stop_loss?: number | null;
  take_profit?: number | null;
  risk_reward?: number | null;
  risk_percent?: number | null;
  validation_status: ValidationStatus;
  reason_codes?: string[];
  warnings?: string[];
  expires_at?: string | null;
}
