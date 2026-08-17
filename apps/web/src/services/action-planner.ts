/**
 * Wave 2B Multi-Step Action Planner & Execution Engine
 * Formulates and coordinates bounded multi-step action sequences with live step tracking,
 * risk-level assessment, confirmation gating, and real-time HUD event broadcasts.
 */

import {
  ActionPlanStep,
  ActionResult,
  MultiStepActionPlan,
} from '../types/actions';
import { actionRuntimeClient } from './actions';
import { browserControlService } from './browser-control';

export class ActionPlannerService {
  private activePlan: MultiStepActionPlan | null = null;
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
   * Synthesizes a structured multi-step plan for a high-level user goal.
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
    this.notify();
    return plan;
  }

  /**
   * Synthesizes a deterministic plan for common browser & visual workflows.
   */
  public createDeterministicWorkflow(type: 'market_research' | 'browse_page' | 'inspect_ui', params: Record<string, string>): MultiStepActionPlan {
    switch (type) {
      case 'market_research': {
        const symbol = params.symbol || 'AAPL';
        return this.createPlan(`Research market setup for ${symbol}`, [
          {
            skill: 'browser',
            action: 'open_url',
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
            action: 'open_url',
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
   * Executes the active plan sequentially. Pauses if a step requires confirmation.
   */
  public async executePlan(): Promise<ActionResult> {
    if (!this.activePlan || this.activePlan.steps.length === 0) {
      return {
        schema_version: '1.0.0',
        request_id: `plan_err_${Date.now()}`,
        status: 'FAILED',
        risk_level: 'READ_ONLY',
        summary: 'No active multi-step plan to execute.',
        data: {},
        error: 'No active plan',
        started_at: new Date().toISOString(),
        completed_at: new Date().toISOString(),
      };
    }

    const plan = this.activePlan;
    plan.status = 'EXECUTING';
    this.notify();

    const startedAt = new Date().toISOString();

    for (let i = plan.current_step_index; i < plan.steps.length; i++) {
      const step = plan.steps[i];
      plan.current_step_index = i;
      step.status = 'RUNNING';
      this.notify();

      try {
        // If step requires confirmation, halt for user approval
        if (step.risk_level === 'CONFIRM_REQUIRED') {
          plan.status = 'AWAITING_CONFIRMATION';
          this.notify();
          return {
            schema_version: '1.0.0',
            request_id: plan.plan_id,
            status: 'CONFIRMATION_REQUIRED',
            risk_level: 'CONFIRM_REQUIRED',
            summary: `Step ${step.step_number} requires user confirmation: ${step.description}`,
            data: {
              plan_id: plan.plan_id,
              step_number: step.step_number,
              action: `${step.skill}.${step.action}`,
              description: step.description,
            },
            started_at: startedAt,
            completed_at: null,
          };
        }

        // Execute step via ActionRuntime or BrowserControl
        const stepResult = await this.executeStep(step);
        step.status = stepResult.status === 'SUCCEEDED' ? 'SUCCEEDED' : 'FAILED';
        step.result_summary = stepResult.summary;

        if (stepResult.status !== 'SUCCEEDED') {
          plan.status = 'FAILED';
          this.notify();
          return stepResult;
        }
      } catch (err) {
        step.status = 'FAILED';
        const errMsg = err instanceof Error ? err.message : String(err);
        step.result_summary = errMsg;
        plan.status = 'FAILED';
        this.notify();
        return {
          schema_version: '1.0.0',
          request_id: plan.plan_id,
          status: 'FAILED',
          risk_level: step.risk_level,
          summary: `Plan execution failed at step ${step.step_number}: ${errMsg}`,
          data: { failed_step: step.step_number },
          error: errMsg,
          started_at: startedAt,
          completed_at: new Date().toISOString(),
        };
      }
    }

    plan.status = 'COMPLETED';
    this.notify();

    return {
      schema_version: '1.0.0',
      request_id: plan.plan_id,
      status: 'SUCCEEDED',
      risk_level: 'LOW_RISK',
      summary: `Successfully completed ${plan.steps.length}-step plan: "${plan.goal}"`,
      data: {
        plan_id: plan.plan_id,
        goal: plan.goal,
        total_steps: plan.steps.length,
      },
      started_at: startedAt,
      completed_at: new Date().toISOString(),
    };
  }

  /**
   * Resumes a paused plan after user confirmation.
   */
  public async resumeAfterConfirmation(approved: boolean): Promise<ActionResult> {
    if (!this.activePlan || this.activePlan.status !== 'AWAITING_CONFIRMATION') {
      return {
        schema_version: '1.0.0',
        request_id: `plan_resume_${Date.now()}`,
        status: 'FAILED',
        risk_level: 'READ_ONLY',
        summary: 'No plan is awaiting confirmation.',
        data: {},
        error: 'Not awaiting confirmation',
        started_at: new Date().toISOString(),
        completed_at: new Date().toISOString(),
      };
    }

    const currentStep = this.activePlan.steps[this.activePlan.current_step_index];
    if (!approved) {
      currentStep.status = 'FAILED';
      currentStep.result_summary = 'User denied confirmation.';
      this.activePlan.status = 'CANCELLED';
      this.notify();

      return {
        schema_version: '1.0.0',
        request_id: this.activePlan.plan_id,
        status: 'DENIED',
        risk_level: 'CONFIRM_REQUIRED',
        summary: `Plan cancelled by user at step ${currentStep.step_number}.`,
        data: { plan_id: this.activePlan.plan_id },
        error: 'User denied confirmation',
        started_at: new Date().toISOString(),
        completed_at: new Date().toISOString(),
      };
    }

    // Step approved, execute step and continue remaining steps
    try {
      const stepRes = await this.executeStep(currentStep);
      currentStep.status = 'SUCCEEDED';
      currentStep.result_summary = stepRes.summary;
      this.activePlan.current_step_index += 1;
      return await this.executePlan();
    } catch (err) {
      currentStep.status = 'FAILED';
      this.activePlan.status = 'FAILED';
      this.notify();
      const errMsg = err instanceof Error ? err.message : String(err);
      return {
        schema_version: '1.0.0',
        request_id: this.activePlan.plan_id,
        status: 'FAILED',
        risk_level: currentStep.risk_level,
        summary: `Error executing confirmed step: ${errMsg}`,
        data: {},
        error: errMsg,
        started_at: new Date().toISOString(),
        completed_at: new Date().toISOString(),
      };
    }
  }

  /**
   * Clears the active plan.
   */
  public clearPlan(): void {
    this.activePlan = null;
    this.notify();
  }

  private async executeStep(step: ActionPlanStep): Promise<ActionResult> {
    if (step.skill === 'browser') {
      if (step.action === 'open_url' || step.action === 'navigate') {
        return await browserControlService.navigate(String(step.arguments.url || ''));
      }
      if (step.action === 'inspect_dom') {
        const ctx = browserControlService.inspectPage();
        return {
          schema_version: '1.0.0',
          request_id: `step_${Date.now()}`,
          status: 'SUCCEEDED',
          risk_level: 'READ_ONLY',
          summary: `Inspected DOM: found ${ctx.dom_tree?.length || 0} interactive elements across ${ctx.headings?.length || 0} sections.`,
          data: { elements_count: ctx.dom_tree?.length || 0, headings: ctx.headings || [] },
          started_at: new Date().toISOString(),
          completed_at: new Date().toISOString(),
        };
      }
      if (step.action === 'click') {
        return await browserControlService.clickElement(String(step.arguments.selector || ''));
      }
      if (step.action === 'type') {
        return await browserControlService.typeText(
          String(step.arguments.selector || ''),
          String(step.arguments.text || '')
        );
      }
      if (step.action === 'read_text') {
        const mode = (typeof step.arguments.mode === 'string' ? step.arguments.mode : 'summary') as 'all' | 'selection' | 'summary' | 'headings';
        const text = browserControlService.readPageText(mode);
        return {
          schema_version: '1.0.0',
          request_id: `step_${Date.now()}`,
          status: 'SUCCEEDED',
          risk_level: 'READ_ONLY',
          summary: `Extracted page text (${text.length} chars)`,
          data: { text_preview: text.substring(0, 150), length: text.length },
          started_at: new Date().toISOString(),
          completed_at: new Date().toISOString(),
        };
      }
    }

    // Default: submit through Action Runtime
    const req = actionRuntimeClient.createRequest({
      skill: step.skill,
      action: step.action,
      arguments: step.arguments,
      source: 'hud',
    });

    return await actionRuntimeClient.submitAction(req);
  }
}

export const actionPlannerService = new ActionPlannerService();
