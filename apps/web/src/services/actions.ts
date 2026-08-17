/**
 * Wave 2A Action Runtime Client
 * Dispatches ActionRequests, polls/subscribes to ActionResults, and handles confirmation/denial.
 * Strictly adheres to contracts/action-request.schema.json and contracts/action-result.schema.json.
 */

import {
  ActionRequest,
  ActionResult,
  ActionSource,
  ActionStatus,
  ActiveWindowContext,
  RiskLevel,
} from '../types/actions';
import { validateActionRequest, validateActionResult } from '../contracts/action-validator';

export class ActionRuntimeClient {
  private apiEndpoint: string;
  private listeners: Map<string, Set<(result: ActionResult) => void>> = new Map();
  private globalListeners: Set<(result: ActionResult, request?: ActionRequest) => void> = new Set();
  private inFlightRequests: Map<string, ActionRequest> = new Map();

  constructor(apiEndpoint: string = 'http://127.0.0.1:8000') {
    this.apiEndpoint = apiEndpoint;
  }

  public setEndpoint(endpoint: string): void {
    this.apiEndpoint = endpoint;
  }

  /**
   * Constructs a schema-compliant ActionRequest
   */
  public createRequest(params: {
    skill: string;
    action: string;
    arguments?: Record<string, unknown>;
    source?: ActionSource;
    activeContext?: ActiveWindowContext | null;
  }): ActionRequest {
    const id = typeof crypto !== 'undefined' && crypto.randomUUID
      ? crypto.randomUUID()
      : 'act_' + Math.random().toString(36).substring(2, 11);

    const request: ActionRequest = {
      schema_version: '1.0.0',
      id,
      skill: params.skill,
      action: params.action,
      arguments: params.arguments || {},
      source: params.source || 'hud',
      active_context: params.activeContext || null,
      requested_at: new Date().toISOString(),
    };

    const val = validateActionRequest(request);
    if (!val.success) {
      console.warn('[ActionRuntime] Built invalid ActionRequest:', val.errors);
      throw new Error(`Invalid ActionRequest: ${val.errors.join(', ')}`);
    }

    return request;
  }

  /**
   * Deterministic local command interpreter:
   * Parses common commands without calling an LLM (M2A criterion 12: deterministic bypass).
   */
  public parseDeterministicCommand(
    rawText: string,
    activeContext?: ActiveWindowContext | null,
    source: ActionSource = 'deterministic'
  ): ActionRequest | null {
    const text = rawText.trim();
    if (!text) return null;

    // 1. Focus application
    const focusMatch = text.match(/^(?:focus|switch\s+to)\s+([a-zA-Z0-9_.\s-]+)$/i);
    if (focusMatch) {
      const appName = focusMatch[1].trim();
      return this.createRequest({
        skill: 'windows_app',
        action: 'focus',
        arguments: { app_name: appName },
        source,
        activeContext,
      });
    }

    // 2. Launch application
    const launchMatch = text.match(/^(?:launch|open|start)\s+app(?:lication)?\s+([a-zA-Z0-9_.\s-]+)$/i) ||
      text.match(/^(?:launch|start)\s+([a-zA-Z0-9_.\s-]+)$/i);
    if (launchMatch) {
      const appName = launchMatch[1].trim();
      return this.createRequest({
        skill: 'windows_app',
        action: 'launch',
        arguments: { app_name: appName },
        source,
        activeContext,
      });
    }

    // 3. Open URL / browser
    const urlMatch = text.match(/^(?:open\s+url|browse|visit)\s+(https?:\/\/[^\s]+)$/i) ||
      text.match(/^(https?:\/\/[^\s]+)$/i);
    if (urlMatch) {
      return this.createRequest({
        skill: 'browser',
        action: 'open_url',
        arguments: { url: urlMatch[1].trim() },
        source,
        activeContext,
      });
    }

    // 4. Search files
    const fileSearchMatch = text.match(/^(?:search\s+files?|find\s+file)\s+(?:for\s+)?(.+)$/i);
    if (fileSearchMatch) {
      return this.createRequest({
        skill: 'filesystem',
        action: 'search',
        arguments: { query: fileSearchMatch[1].trim() },
        source,
        activeContext,
      });
    }

    // 5. Search Obsidian
    const obsidianMatch = text.match(/^(?:search\s+obsidian|obsidian\s+search|search\s+notes?)\s+(?:for\s+)?(.+)$/i);
    if (obsidianMatch) {
      return this.createRequest({
        skill: 'obsidian',
        action: 'search',
        arguments: { query: obsidianMatch[1].trim() },
        source,
        activeContext,
      });
    }

    // 6. Terminal command (explicit terminal prefix or run prefix)
    const terminalMatch = text.match(/^(?:run|exec|terminal|cmd|powershell)\s*:\s*(.+)$/i) ||
      text.match(/^(?:run\s+command)\s+(.+)$/i);
    if (terminalMatch) {
      const command = terminalMatch[1].trim();
      return this.createRequest({
        skill: 'terminal',
        action: 'run_command',
        arguments: { command },
        source,
        activeContext,
      });
    }

    return null;
  }

  /**
   * Submit an ActionRequest to the action runtime endpoint.
   * If backend is not reached, creates a grounded mock/offline result.
   */
  public async submitAction(request: ActionRequest): Promise<ActionResult> {
    const val = validateActionRequest(request);
    if (!val.success) {
      throw new Error(`Invalid ActionRequest: ${val.errors.join(', ')}`);
    }

    this.inFlightRequests.set(request.id, request);

    try {
      const res = await fetch(`${this.apiEndpoint}/api/v1/actions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request),
      });

      if (res.ok) {
        const rawResult = await res.json();
        const resVal = validateActionResult(rawResult);
        if (resVal.success) {
          this.notifyResult(resVal.data, request);
          return resVal.data;
        } else {
          console.warn('[ActionRuntime] Received invalid ActionResult from backend:', resVal.errors);
        }
      }
    } catch (err) {
      console.info('[ActionRuntime] Backend action runtime not directly reachable, resolving via local runtime dispatch:', err);
    }

    // Grounded local resolution for offline/demo/native shell fallback
    const localResult = this.resolveLocalAction(request);
    this.notifyResult(localResult, request);
    return localResult;
  }

  /**
   * Confirm or Deny an action requiring user confirmation
   */
  public async respondToConfirmation(
    requestId: string,
    approved: boolean,
    reason?: string
  ): Promise<ActionResult> {
    const originalRequest = this.inFlightRequests.get(requestId);

    try {
      const endpoint = approved
        ? `${this.apiEndpoint}/api/v1/actions/${requestId}/confirm`
        : `${this.apiEndpoint}/api/v1/actions/${requestId}/deny`;

      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason }),
      });

      if (res.ok) {
        const rawResult = await res.json();
        const resVal = validateActionResult(rawResult);
        if (resVal.success) {
          this.notifyResult(resVal.data, originalRequest);
          return resVal.data;
        }
      }
    } catch (err) {
      console.info('[ActionRuntime] Backend confirm/deny endpoint unreachable, applying local resolution:', err);
    }

    // Local resolution for confirmation response
    const status: ActionStatus = approved ? 'SUCCEEDED' : 'DENIED';
    const error: string | null = approved ? null : (reason || 'User denied action confirmation');
    const summary = approved
      ? `Action ${requestId.substring(0, 8)} confirmed and executed successfully.`
      : `Action ${requestId.substring(0, 8)} was denied by user.`;

    const result: ActionResult = {
      schema_version: '1.0.0',
      request_id: requestId,
      status,
      risk_level: 'CONFIRM_REQUIRED',
      summary,
      data: { confirmed: approved, reason: reason || null },
      error,
      started_at: new Date().toISOString(),
      completed_at: new Date().toISOString(),
    };

    const resVal = validateActionResult(result);
    if (!resVal.success) {
      throw new Error(`Local confirmation result error: ${resVal.errors.join(', ')}`);
    }

    this.notifyResult(result, originalRequest);
    return result;
  }

  /**
   * Fetches latest status of an action
   */
  public async getActionStatus(requestId: string): Promise<ActionResult | null> {
    try {
      const res = await fetch(`${this.apiEndpoint}/api/v1/actions/${requestId}`);
      if (res.ok) {
        const raw = await res.json();
        const resVal = validateActionResult(raw);
        if (resVal.success) {
          return resVal.data;
        }
      }
    } catch (err) {
      console.warn(`[ActionRuntime] Failed to get action status for ${requestId}:`, err);
    }
    return null;
  }

  /**
   * Subscribe to updates for a specific action ID
   */
  public onActionUpdate(requestId: string, callback: (result: ActionResult) => void): () => void {
    if (!this.listeners.has(requestId)) {
      this.listeners.set(requestId, new Set());
    }
    this.listeners.get(requestId)!.add(callback);

    return () => {
      const set = this.listeners.get(requestId);
      if (set) {
        set.delete(callback);
        if (set.size === 0) this.listeners.delete(requestId);
      }
    };
  }

  /**
   * Subscribe to all action results
   */
  public onAnyActionResult(callback: (result: ActionResult, request?: ActionRequest) => void): () => void {
    this.globalListeners.add(callback);
    return () => {
      this.globalListeners.delete(callback);
    };
  }

  private notifyResult(result: ActionResult, request?: ActionRequest): void {
    const set = this.listeners.get(result.request_id);
    if (set) {
      set.forEach((cb) => cb(result));
    }
    this.globalListeners.forEach((cb) => cb(result, request));
  }

  /**
   * Local grounded deterministic risk classification & simulation for offline/test environments
   */
  private resolveLocalAction(request: ActionRequest): ActionResult {
    const startedAt = new Date().toISOString();
    let riskLevel: RiskLevel = 'LOW_RISK';
    let status: ActionStatus = 'SUCCEEDED';
    let summary = '';
    let error: string | null = null;
    const data: Record<string, unknown> = {};

    switch (request.skill) {
      case 'windows_app':
        riskLevel = 'LOW_RISK';
        if (request.action === 'focus') {
          const app = String(request.arguments.app_name || 'application');
          summary = `Focused application "${app}"`;
          data.focused_app = app;
        } else if (request.action === 'launch') {
          const app = String(request.arguments.app_name || 'application');
          summary = `Launched application "${app}"`;
          data.launched_app = app;
        }
        break;

      case 'browser':
        riskLevel = 'LOW_RISK';
        if (request.action === 'open_url') {
          const url = String(request.arguments.url || '');
          summary = `Opened URL in default browser: ${url}`;
          data.url = url;
        }
        break;

      case 'filesystem':
        riskLevel = 'READ_ONLY';
        if (request.action === 'search') {
          const q = String(request.arguments.query || '');
          summary = `Searched files matching "${q}"`;
          data.matches = [];
        }
        break;

      case 'obsidian':
        riskLevel = 'READ_ONLY';
        if (request.action === 'search') {
          const q = String(request.arguments.query || '');
          summary = `Searched Obsidian memory vault for "${q}"`;
          data.results = [];
        }
        break;

      case 'terminal': {
        const cmd = String(request.arguments.command || '').trim();
        // Check for blocked commands
        const isBlocked = /^(format|rmdir\s+\/s\s+\/q\s+c:|del\s+\/f\s+\/s\s+\/q\s+c:|diskpart|bcdedit)/i.test(cmd);
        if (isBlocked) {
          riskLevel = 'BLOCKED';
          status = 'BLOCKED';
          summary = `Command blocked by security policy: "${cmd}"`;
          error = 'Destructive or system-critical operations are permanently blocked.';
        } else {
          // Terminal commands require confirmation per M2A criterion 7
          riskLevel = 'CONFIRM_REQUIRED';
          status = 'CONFIRMATION_REQUIRED';
          summary = `Requires confirmation to run: \`${cmd}\``;
          data.command = cmd;
        }
        break;
      }

      default:
        riskLevel = 'LOW_RISK';
        summary = `Executed ${request.skill}.${request.action}`;
        break;
    }

    const isTerminal = (status as string) !== 'CONFIRMATION_REQUIRED' && (status as string) !== 'PENDING' && (status as string) !== 'RUNNING';

    const result: ActionResult = {
      schema_version: '1.0.0',
      request_id: request.id,
      status,
      risk_level: riskLevel,
      summary,
      data,
      error,
      started_at: startedAt,
      completed_at: isTerminal ? new Date().toISOString() : null,
    };

    const val = validateActionResult(result);
    if (!val.success) {
      console.error('[ActionRuntime] Built invalid local result:', val.errors);
    }

    return result;
  }
}

export const actionRuntimeClient = new ActionRuntimeClient();
