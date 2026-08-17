/**
 * Wave 2B Multi-Step Action Planner
 * Formulates bounded multi-step action sequences and coordinates their
 * execution with live step tracking and real-time HUD event broadcasts.
 *
 * This service has NO independent authority: it does not classify risk,
 * decide whether a step requires confirmation, decide a step succeeded, or
 * retry anything on its own. Every step is submitted to the backend Codex
 * Action Runtime via `actionRuntimeClient` (already the sole
 * permission/execution/audit authority -- see `services/actions.ts`), and
 * this service only reacts to the real `ActionResult.status` the backend
 * returns. A step's client-supplied `risk_level` is a display hint only
 * (mirrors `apps/backend/actions/plan_models.py`'s `ActionStep.risk_level`
 * docstring: "the runtime always replaces it with
 * PermissionEngine.classify() before making an execution decision") --
 * never read here to gate execution or confirmation.
 *
 * DOM-level browser steps are NOT executed locally against `document`.
 * They are submitted like any other action; the backend's `browser` skill
 * dispatches the already-authorized command back out to this same
 * renderer via `services/frontend-command-bridge.ts`, which is the only
 * place `browser-control.ts`'s mutating methods are invoked from.
 */

import {
  ActionPlanStep,
  ActionResult,
  MultiStepActionPlan,
} from '../types/actions';
import { actionRuntimeClient } from './actions';

interface PendingConfirmation {
  requestId: string;
  confirmationToken: string;
}

export class ActionPlannerService {
  private activePlan: MultiStepActionPlan | null = null;
  private pendingConfirmation: PendingConfirmation | null = null;
  private planListeners: Set<(plan: MultiStepActionPlan | null) => void> = new Set();

  /**
   * Returns the current active plan if any.
   */
  public getActivePlan(): MultiStepActionPlan | null {
    return this.activePlan ? JSON.parse(JSON.stringify(this.activePlan)) : null;
  }

  /**
   * Subscribes to plan state updates.
   */
  public onPlanUpdate(callback: (plan: MultiStepActionPlan | null) => void): () => void {
    this.planListeners.add(callback);
    return () => {
      this.planListeners.delete(callback);
    };
  }

  private notify(): void {
    const copy = this.getActivePlan();
    this.planListeners.forEach((cb) => cb(copy));
  }

  /**
   * Synthesizes a structured multi-step plan proposal for a high-level user
   * goal. Proposing a plan is display/UX state only -- nothing executes
   * until executePlan() submits each step to the backend.
   */
  public createPlan(goal: string, steps: Omit<ActionPlanStep, 'step_number' | 'status'>[]): MultiStepActionPlan {
    const planId = `plan_${Date.now()}_${Math.random().toString(36).substring(2, 7)}`;
    const planSteps: ActionPlanStep[] = steps.map((s, idx) => ({
      step_number: idx + 1,
      skill: s.skill,
      action: s.action,
      description: s.description,
      arguments: s.arguments || {},
      risk_level: s.risk_level || 'LOW_RISK',
      status: 'PENDING',
      result_summary: undefined,
    }));

    const plan: MultiStepActionPlan = {
      plan_id: planId,
      goal,
      steps: planSteps,
      current_step_index: 0,
      status: 'PLANNING',
      created_at: new Date().toISOString(),
    };

    this.activePlan = plan;
    this.pendingConfirmation = null;
    this.notify();
    return plan;
  }

  /**
   * Synthesizes a deterministic plan proposal for common browser & visual
   * workflows.
   */
  public createDeterministicWorkflow(type: 'market_research' | 'browse_page' | 'inspect_ui', params: Record<string, string>): MultiStepActionPlan {
    switch (type) {
      case 'market_research': {
        const symbol = params.symbol || 'AAPL';
        return this.createPlan(`Research market setup for ${symbol}`, [
          {
            skill: 'browser',
            action: 'navigate',
            description: `Open TradingView chart for ${symbol}`,
            arguments: { url: `https://tradingview.com/symbols/${encodeURIComponent(symbol)}` },
            risk_level: 'LOW_RISK',
          },
          {
            skill: 'browser',
            action: 'inspect_dom',
            description: 'Inspect page elements and market chart structure',
            arguments: {},
            risk_level: 'READ_ONLY',
          },
          {
            skill: 'obsidian',
            action: 'search',
            description: `Cross-reference research notes for ${symbol}`,
            arguments: { query: `${symbol} strategy risk rules` },
            risk_level: 'READ_ONLY',
          },
        ]);
      }

      case 'browse_page': {
        const url = params.url || 'https://news.ycombinator.com';
        return this.createPlan(`Navigate and inspect ${url}`, [
          {
            skill: 'browser',
            action: 'navigate',
            description: `Navigate to ${url}`,
            arguments: { url },
            risk_level: 'LOW_RISK',
          },
          {
            skill: 'browser',
            action: 'inspect_dom',
            description: 'Inspect DOM interactive tree',
            arguments: {},
            risk_level: 'READ_ONLY',
          },
          {
            skill: 'browser',
            action: 'read_text',
            description: 'Extract readable summary of page text',
            arguments: { mode: 'summary' },
            risk_level: 'READ_ONLY',
          },
        ]);
      }

      case 'inspect_ui': {
        return this.createPlan('Capture screen awareness and active window UI hierarchy', [
          {
            skill: 'windows_app',
            action: 'get_monitors',
            description: 'Query monitor geometry and DPI scaling',
            arguments: {},
            risk_level: 'READ_ONLY',
          },
          {
            skill: 'windows_app',
            action: 'capture_active_window',
            description: 'Capture active foreground window screenshot',
            arguments: { include_image_data: true },
            risk_level: 'READ_ONLY',
          },
          {
            skill: 'windows_app',
            action: 'get_ui_elements',
            description: 'Inspect Win32 accessibility UI hierarchy',
            arguments: {},
            risk_level: 'READ_ONLY',
          },
        ]);
      }
    }
  }

  /**
   * Executes the active plan sequentially, submitting each step to the
   * backend Action Runtime and reacting only to its real ActionResult.
   * Halts for user approval exactly when the backend -- not this service --
   * reports CONFIRMATION_REQUIRED.
   */
  public async executePlan(): Promise<ActionResult> {
    if (!this.activePlan || this.activePlan.steps.length === 0) {
      return this.syntheticResult('FAILED', 'No active multi-step plan to execute.', 'No active plan');
    }

    const plan = this.activePlan;
    plan.status = 'EXECUTING';
    this.notify();

    for (let i = plan.current_step_index; i < plan.steps.length; i++) {
      const step = plan.steps[i];
      plan.current_step_index = i;
      step.status = 'RUNNING';
      this.notify();

      let stepResult: ActionResult;
      try {
        stepResult = await this.submitStep(step);
      } catch (err) {
        const errMsg = err instanceof Error ? err.message : String(err);
        step.status = 'FAILED';
        step.result_summary = errMsg;
        plan.status = 'FAILED';
        this.notify();
        return this.syntheticResult(
          'FAILED',
          `Plan execution failed at step ${step.step_number}: ${errMsg}`,
          errMsg
        );
      }

      if (stepResult.status === 'CONFIRMATION_REQUIRED') {
        const token = stepResult.data?.confirmation_token;
        if (typeof token !== 'string' || !token) {
          // The backend asked for confirmation but gave us nothing to confirm
          // with -- this is a real integration failure, not something to
          // paper over by inventing a token or treating it as approved.
          step.status = 'FAILED';
          step.result_summary = 'Backend requested confirmation without a confirmation_token.';
          plan.status = 'FAILED';
          this.notify();
          return stepResult;
        }
        this.pendingConfirmation = { requestId: stepResult.request_id, confirmationToken: token };
        plan.status = 'AWAITING_CONFIRMATION';
        step.result_summary = stepResult.summary;
        this.notify();
        return stepResult;
      }

      step.status = stepResult.status === 'SUCCEEDED' ? 'SUCCEEDED' : 'FAILED';
      step.result_summary = stepResult.summary;

      if (stepResult.status !== 'SUCCEEDED') {
        plan.status = 'FAILED';
        this.notify();
        return stepResult;
      }
    }

    plan.status = 'COMPLETED';
    this.notify();

    return this.syntheticResult(
      'SUCCEEDED',
      `Successfully completed ${plan.steps.length}-step plan: "${plan.goal}"`,
      null,
      { plan_id: plan.plan_id, goal: plan.goal, total_steps: plan.steps.length }
    );
  }

  /**
   * Resumes a paused plan after user confirmation. The approval decision and
   * its execution both happen on the backend via the real confirmation
   * endpoint -- this service never re-executes the step itself.
   */
  public async resumeAfterConfirmation(approved: boolean): Promise<ActionResult> {
    if (!this.activePlan || this.activePlan.status !== 'AWAITING_CONFIRMATION' || !this.pendingConfirmation) {
      return this.syntheticResult('FAILED', 'No plan is awaiting confirmation.', 'Not awaiting confirmation');
    }

    const { requestId, confirmationToken } = this.pendingConfirmation;
    const currentStep = this.activePlan.steps[this.activePlan.current_step_index];

    const result = await actionRuntimeClient.respondToConfirmation(requestId, confirmationToken, approved);
    this.pendingConfirmation = null;

    if (!approved || result.status === 'DENIED') {
      currentStep.status = 'FAILED';
      currentStep.result_summary = result.summary || 'User denied confirmation.';
      this.activePlan.status = 'CANCELLED';
      this.notify();
      return result;
    }

    if (result.status !== 'SUCCEEDED') {
      currentStep.status = 'FAILED';
      currentStep.result_summary = result.summary;
      this.activePlan.status = 'FAILED';
      this.notify();
      return result;
    }

    currentStep.status = 'SUCCEEDED';
    currentStep.result_summary = result.summary;
    this.activePlan.current_step_index += 1;
    this.notify();
    return await this.executePlan();
  }

  /**
   * Clears the active plan.
   */
  public clearPlan(): void {
    this.activePlan = null;
    this.pendingConfirmation = null;
    this.notify();
  }

  /**
   * Submits one step to the backend Action Runtime -- the sole path for
   * every skill, including `browser`'s DOM actions. No local execution, no
   * local risk classification.
   */
  private async submitStep(step: ActionPlanStep): Promise<ActionResult> {
    const req = actionRuntimeClient.createRequest({
      skill: step.skill,
      action: step.action,
      arguments: step.arguments,
      source: 'hud',
    });

    return await actionRuntimeClient.submitAction(req);
  }

  private syntheticResult(
    status: ActionResult['status'],
    summary: string,
    error: string | null,
    data: Record<string, unknown> = {}
  ): ActionResult {
    const now = new Date().toISOString();
    return {
      schema_version: '1.0.0',
      request_id: `plan_${status.toLowerCase()}_${Date.now()}`,
      status,
      risk_level: 'READ_ONLY',
      summary,
      data,
      error,
      started_at: now,
      completed_at: now,
    };
  }
}

export const actionPlannerService = new ActionPlannerService();
