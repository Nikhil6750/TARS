/**
 * Executes commands the backend Codex Action Runtime has already validated,
 * risk-classified, and (if required) had the user confirm -- see
 * `apps/backend/actions/frontend_bridge.py`. This module has no
 * independent authority: it never decides whether a command runs, only
 * performs the literal action the backend dispatched and truthfully
 * reports what happened.
 *
 * Listens on the same `/ws/actions` stream `actions.ts` already reports
 * ActionResults over, for `{"type": "frontend_command", request_id, skill,
 * action, arguments}` messages, then POSTs a real, truthful outcome to
 * `POST /api/v1/actions/{request_id}/frontend-report`.
 */

import { browserControlService } from './browser-control';
import { nativeBridge } from './native-bridge';
import { ActionResult } from '../types/actions';

interface FrontendCommandMessage {
  type: 'frontend_command';
  request_id: string;
  skill: string;
  action: string;
  arguments: Record<string, unknown>;
}

interface ReportPayload {
  success: boolean;
  data?: Record<string, unknown>;
  error?: string;
}

function isFrontendCommand(value: unknown): value is FrontendCommandMessage {
  if (typeof value !== 'object' || value === null) return false;
  const msg = value as Record<string, unknown>;
  return (
    msg.type === 'frontend_command' &&
    typeof msg.request_id === 'string' &&
    typeof msg.skill === 'string' &&
    typeof msg.action === 'string' &&
    typeof msg.arguments === 'object' &&
    msg.arguments !== null
  );
}

function fromActionResult(result: ActionResult): ReportPayload {
  if (result.status !== 'SUCCEEDED') {
    return { success: false, error: result.error || result.summary };
  }
  return { success: true, data: { ...result.data, summary: result.summary } };
}

export class FrontendCommandBridge {
  private ws: WebSocket | null = null;
  private wsUrl = '';
  private reportUrl = '';
  private reconnectTimer: number | null = null;
  private intentionalClose = false;

  public connect(apiEndpoint: string): void {
    const wsUrl = apiEndpoint.replace(/^http/, 'ws').replace(/\/$/, '') + '/ws/actions';
    const reportUrl = apiEndpoint.replace(/\/$/, '');
    if (this.ws && this.wsUrl === wsUrl && this.ws.readyState === WebSocket.OPEN) {
      return;
    }
    this.disconnect();
    this.wsUrl = wsUrl;
    this.reportUrl = reportUrl;
    this.intentionalClose = false;
    this.open();
  }

  public disconnect(): void {
    this.intentionalClose = true;
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        // ignore
      }
      this.ws = null;
    }
  }

  private open(): void {
    try {
      this.ws = new WebSocket(this.wsUrl);
    } catch (err) {
      console.warn('[FrontendCommandBridge] Failed to open WebSocket:', err);
      this.scheduleReconnect();
      return;
    }

    this.ws.onmessage = (evt: MessageEvent) => {
      this.handleMessage(evt.data);
    };
    this.ws.onclose = () => {
      if (!this.intentionalClose) this.scheduleReconnect();
    };
    this.ws.onerror = () => {
      // onclose follows; reconnection is handled there.
    };
  }

  private scheduleReconnect(): void {
    if (this.intentionalClose || this.reconnectTimer !== null) return;
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      if (!this.intentionalClose) this.open();
    }, 3000);
  }

  private handleMessage(raw: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      return;
    }
    if (!isFrontendCommand(parsed)) return;
    void this.executeAndReport(parsed);
  }

  private async executeAndReport(msg: FrontendCommandMessage): Promise<void> {
    let payload: ReportPayload;
    try {
      payload = await this.execute(msg.skill, msg.action, msg.arguments);
    } catch (err) {
      payload = { success: false, error: err instanceof Error ? err.message : String(err) };
    }
    await this.report(msg.request_id, payload);
  }

  private async execute(
    skill: string,
    action: string,
    args: Record<string, unknown>
  ): Promise<ReportPayload> {
    if (skill === 'browser') return this.executeBrowser(action, args);
    if (skill === 'windows_app') return this.executeWindowsApp(action, args);
    return { success: false, error: `No frontend handler for skill '${skill}'` };
  }

  private async executeBrowser(action: string, args: Record<string, unknown>): Promise<ReportPayload> {
    switch (action) {
      case 'navigate':
        return fromActionResult(await browserControlService.navigate(String(args.url || '')));
      case 'back':
        return fromActionResult(await browserControlService.back());
      case 'forward':
        return fromActionResult(await browserControlService.forward());
      case 'scroll': {
        const deltaY = args.deltaY as number | 'top' | 'bottom' | 'element';
        const elementTarget = typeof args.elementTarget === 'string' ? args.elementTarget : undefined;
        return fromActionResult(await browserControlService.scroll(deltaY, elementTarget));
      }
      case 'click':
        return fromActionResult(
          await browserControlService.clickElement(String(args.target || args.selector || ''))
        );
      case 'type':
        return fromActionResult(
          await browserControlService.typeText(
            String(args.selector || ''),
            String(args.text || ''),
            args.clearFirst !== false
          )
        );
      case 'inspect_dom': {
        const ctx = browserControlService.inspectPage();
        return {
          success: true,
          data: {
            summary: `Inspected DOM: found ${ctx.dom_tree?.length || 0} interactive elements.`,
            dom_tree: ctx.dom_tree,
            headings: ctx.headings,
            elements_count: ctx.dom_tree?.length || 0,
          },
        };
      }
      case 'read_text': {
        const mode = (typeof args.mode === 'string' ? args.mode : 'summary') as
          | 'all'
          | 'selection'
          | 'summary'
          | 'headings';
        const text = browserControlService.readPageText(mode);
        return {
          success: true,
          data: { summary: `Extracted page text (${text.length} chars).`, text, length: text.length },
        };
      }
      default:
        return { success: false, error: `Unknown browser action '${action}'` };
    }
  }

  private async executeWindowsApp(action: string, args: Record<string, unknown>): Promise<ReportPayload> {
    switch (action) {
      case 'capture_active_window': {
        const includeImageData = args.include_image_data !== false;
        const capture = await nativeBridge.captureActiveWindow(includeImageData);
        if (capture.error) {
          return { success: false, error: capture.error, data: { ...capture } };
        }
        return {
          success: true,
          data: { ...capture, summary: `Captured active window (${capture.width}x${capture.height}).` },
        };
      }
      case 'get_monitors': {
        const monitors = await nativeBridge.getMonitorsGeometry();
        return {
          success: true,
          data: { monitors, summary: `Enumerated ${monitors.length} monitor(s).` },
        };
      }
      case 'get_ui_elements': {
        const tree = await nativeBridge.getActiveWindowElements();
        return {
          success: true,
          data: { ui_tree: tree, summary: `Captured UI element tree rooted at '${tree.name}'.` },
        };
      }
      default:
        return { success: false, error: `Unknown windows_app action '${action}'` };
    }
  }

  private async report(requestId: string, payload: ReportPayload): Promise<void> {
    try {
      await fetch(`${this.reportUrl}/api/v1/actions/${requestId}/frontend-report`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
    } catch (err) {
      console.warn('[FrontendCommandBridge] Failed to report command result:', err);
    }
  }
}

export const frontendCommandBridge = new FrontendCommandBridge();
