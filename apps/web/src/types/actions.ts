/**
 * Wave 2A Shared Action & Skill Contracts for TARS HUD & Native Shell
 * Directly mirrors contracts/action-request.schema.json and contracts/action-result.schema.json
 */

export type RiskLevel = 'READ_ONLY' | 'LOW_RISK' | 'CONFIRM_REQUIRED' | 'BLOCKED';

export type ActionSource = 'hud' | 'voice_ptt' | 'voice_wake_word' | 'hotkey' | 'deterministic' | 'api';

export type ActionStatus =
  | 'PENDING'
  | 'CONFIRMATION_REQUIRED'
  | 'DENIED'
  | 'BLOCKED'
  | 'RUNNING'
  | 'SUCCEEDED'
  | 'FAILED';

export interface WindowBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface ActiveWindowContext {
  executable: string;
  process_id?: number | null;
  window_title: string;
  window_bounds?: WindowBounds | null;
  captured_at?: string | null;
}

export interface ActionRequest {
  schema_version: '1.0.0';
  id: string;
  skill: string;
  action: string;
  arguments: Record<string, unknown>;
  source: ActionSource;
  active_context?: ActiveWindowContext | null;
  requested_at: string;
}

export interface ActionResult {
  schema_version: '1.0.0';
  request_id: string;
  status: ActionStatus;
  risk_level?: RiskLevel | null;
  summary: string;
  data: Record<string, unknown>;
  error?: string | null;
  started_at: string;
  completed_at?: string | null;
}

export interface ActionStateRecord {
  request: ActionRequest;
  result?: ActionResult;
}
