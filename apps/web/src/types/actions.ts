/**
 * Wave 2A / Wave 2B Shared Action, Vision & Browser Contracts for TARS HUD & Native Shell
 * Strictly adheres to contracts/action-request.schema.json and contracts/action-result.schema.json
 */

export type RiskLevel = 'READ_ONLY' | 'LOW_RISK' | 'CONFIRM_REQUIRED' | 'BLOCKED';

export type ActionSource = 'hud' | 'voice_ptt' | 'voice_wake_word' | 'hotkey' | 'deterministic' | 'api' | 'browser_automation';

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

export interface MonitorInfo {
  id: string;
  name: string;
  is_primary: boolean;
  bounds: WindowBounds;
  work_area: WindowBounds;
  scale_factor: number;
  dpi: number;
}

export interface ScreenCaptureResult {
  capture_id: string;
  captured_at: string;
  source: 'active_window' | 'region' | 'monitor';
  executable: string;
  window_title: string;
  bounds: WindowBounds;
  scale_factor: number;
  dpi: number;
  width: number;
  height: number;
  is_secure_desktop: boolean;
  image_format: string;
  image_data_base64?: string | null;
  temp_file_path?: string | null;
  error?: string | null;
  /** The captured window's own HWND as a string (active_window captures
   * with a real target only) -- lets the backend look up HotChartState
   * for this exact window (TARS Alexa-Speed Phase D). */
  window_id?: string | null;
}

export interface UIElementNode {
  id: string;
  name: string;
  role: string;
  class_name: string;
  bounds?: WindowBounds | null;
  is_enabled: boolean;
  is_visible: boolean;
  children: UIElementNode[];
}

export interface DOMElementSummary {
  id?: string;
  selector: string;
  tag: string;
  role?: string;
  text: string;
  placeholder?: string;
  type?: string;
  bounds?: WindowBounds;
  is_interactive: boolean;
  is_sensitive: boolean;
  is_visible: boolean;
  attributes: Record<string, string>;
}

export interface BrowserTabInfo {
  id: string;
  title: string;
  url: string;
  is_active: boolean;
  index: number;
}

export interface BrowserPageContext {
  url: string;
  title: string;
  tab_id?: string;
  tab_index?: number;
  tab_count?: number;
  is_loading: boolean;
  can_go_back: boolean;
  can_go_forward: boolean;
  selected_text?: string;
  dom_tree?: DOMElementSummary[];
  headings?: string[];
  links_count?: number;
  inputs_count?: number;
  buttons_count?: number;
  captured_at: string;
}

export interface SemanticCriteria {
  text?: string;
  role?: string;
  tag?: string;
  placeholder?: string;
  aria_label?: string;
  selector?: string;
  id?: string;
}

export interface VisualTargetQuery {
  query: string;
  semantic_criteria?: SemanticCriteria;
  coordinate_hint?: { x: number; y: number };
}

export interface TargetingResolution {
  target_type: 'semantic_dom' | 'accessibility_element' | 'visual_coordinate' | 'unresolved';
  element?: DOMElementSummary | UIElementNode | null;
  coordinates?: { x: number; y: number } | null;
  proposed_action: {
    skill: string;
    action: string;
    arguments: Record<string, unknown>;
    risk_level: RiskLevel;
    description: string;
  };
  confidence_rationale: string;
}

export interface ActionPlanStep {
  step_number: number;
  skill: string;
  action: string;
  description: string;
  arguments: Record<string, unknown>;
  risk_level: RiskLevel;
  status: 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'SKIPPED';
  result_summary?: string;
}

export interface MultiStepActionPlan {
  plan_id: string;
  goal: string;
  steps: ActionPlanStep[];
  current_step_index: number;
  status: 'PLANNING' | 'EXECUTING' | 'AWAITING_CONFIRMATION' | 'COMPLETED' | 'FAILED' | 'CANCELLED';
  created_at: string;
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
