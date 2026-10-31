/**
 * Shapes returned by the Market X-Ray API.
 *
 * These mirror the backend's payloads exactly. The dashboard performs no
 * market calculations of its own: it renders what the backend computed, so
 * every number on screen is traceable to one place.
 */

export type FeedStatusName = 'LIVE' | 'STALE' | 'REPLAY' | 'CLOSED' | 'NO_DATA';

export interface MarketStatus {
  status: FeedStatusName;
  market_open: boolean;
  as_of: string;
  data_as_of: string | null;
  data_age_seconds: number | null;
  sessions_active: string[];
  next_transition: string | null;
  detail: string;
  replay: boolean;
}

export interface Cursor {
  as_of: string;
  first_bar: string | null;
  last_bar: string | null;
}

export interface XRayPoint {
  ts: string;
  z: number;
  close: number;
  pct_change: number;
}

export interface XRaySeries {
  symbol: string;
  asset_class: string;
  timeframe: string;
  direction: 'BULLISH' | 'BEARISH' | 'FLAT';
  current_z: number;
  current_close: number;
  pct_change: number;
  rank: number;
  points: XRayPoint[];
}

export interface XRaySnapshot {
  timeframe: string;
  return_window: number;
  as_of: string;
  series: XRaySeries[];
  skipped: Record<string, string>;
  measures: string;
}

export interface Contribution {
  pair: string;
  role: 'BASE' | 'QUOTE';
  sign: number;
  pair_z: number;
  contribution: number;
  pct_change: number;
}

export interface CurrencyStrength {
  currency: string;
  score: number;
  direction: 'BULLISH' | 'BEARISH' | 'NEUTRAL';
  pairs_used: number;
  pairs_possible: number;
  counterparties: string[];
  coverage_ratio: number;
  thin_coverage: boolean;
  leave_one_out_max_delta: number;
  leave_one_out_driver: string | null;
  contributions: Contribution[];
}

export interface StrengthSnapshot {
  window: string;
  timeframe: string;
  bars: number;
  as_of: string;
  ranking: CurrencyStrength[];
  pairs_evaluated: string[];
  pairs_missing: string[];
  method: string;
}

export interface NearestZone {
  zone_id: string;
  side: 'BULLISH' | 'BEARISH';
  status: ZoneStatus;
  zone_low: number;
  zone_high: number;
  distance_atr: number | null;
  score: number;
}

export interface LeaderCard {
  symbol: string;
  asset_class: string;
  close: number;
  pct_change: number;
  z: number;
  rank: number;
  direction: string;
  atr: number | null;
  regime: string;
  spread: number | null;
  nearest_zone: NearestZone | null;
}

export interface LeadersSnapshot {
  timeframe: string;
  as_of: string;
  bullish: LeaderCard[];
  bearish: LeaderCard[];
  measures: string;
}

export interface Candle {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  spread: number;
  volume: number;
}

export interface Regime {
  atr: number | null;
  atr_pct_of_price: number | null;
  percentile: number | null;
  label: string;
}

export interface ChartPayload {
  symbol: string;
  timeframe: string;
  as_of: string;
  candles: Candle[];
  regime?: Regime;
  normalized_return?: {
    raw_return: number;
    pct_change: number;
    volatility: number;
    z: number;
    bars_used: number;
  } | null;
  precision?: number;
  error?: string;
}

export type ZoneStatus = 'FRESH' | 'TAPPED' | 'MITIGATED' | 'INVALIDATED' | 'EXPIRED';

export interface ZoneEvent {
  event_id: string;
  zone_id: string;
  symbol: string;
  timeframe: string;
  kind: string;
  ts: string;
  price: number;
}

export interface OrderBlock {
  zone_id: string;
  symbol: string;
  timeframe: string;
  side: 'BULLISH' | 'BEARISH';
  zone_low: number;
  zone_high: number;
  midpoint: number;
  origin_ts: string;
  confirmed_ts: string;
  displacement_atr: number;
  bos_level: number;
  status: ZoneStatus;
  age_bars: number;
  distance_atr: number | null;
  distance_price: number | null;
  score: number;
  score_basis: string;
  config_version: string;
  events: ZoneEvent[];
}

export interface OrderBlocksPayload {
  symbol: string;
  timeframe: string;
  as_of: string;
  current_price: number | null;
  zones: OrderBlock[];
  active_count: number;
  nearest: OrderBlock | null;
  events_recorded_new: number;
  config: Record<string, unknown>;
}

export interface StoredEvent {
  event_id: string;
  zone_id: string;
  symbol: string;
  timeframe: string;
  kind: string;
  ts: string;
  price: number;
  recorded_at: string;
}

export interface EventsPayload {
  events: StoredEvent[];
  total_stored: number;
  zones_stored: number;
}

export interface Instrument {
  symbol: string;
  asset_class: string;
  base: string;
  quote: string;
  price_precision: number;
  pip_size: number;
  bars_m5: number;
  first_bar: string | null;
  last_bar: string | null;
}

export interface Dashboard {
  status: MarketStatus;
  cursor: Cursor;
  timeframe: string;
  timeframes: string[];
  selected_symbol: string;
  instruments: Instrument[];
  xray: XRaySnapshot;
  strength: StrengthSnapshot;
  leaders: LeadersSnapshot;
  chart: ChartPayload;
  order_blocks: OrderBlocksPayload;
  events: EventsPayload;
  config: Record<string, unknown>;
}
