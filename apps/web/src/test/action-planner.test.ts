import { describe, it, expect, beforeEach, vi } from 'vitest';
import { actionPlannerService } from '../services/action-planner';
import { actionRuntimeClient } from '../services/actions';
import { ActionResult } from '../types/actions';

function result(overrides: Partial<ActionResult>): ActionResult {
  return {
    schema_version: '1.0.0',
    request_id: 'req_1',
    status: 'SUCCEEDED',
    risk_level: 'LOW_RISK',
    summary: 'ok',
    data: {},
    error: null,
    started_at: new Date().toISOString(),
    completed_at: new Date().toISOString(),
    ...overrides,
  };
}

describe('Wave 2B Multi-Step Action Planner', () => {
  beforeEach(() => {
    actionPlannerService.clearPlan();
    vi.restoreAllMocks();
  });

  describe('Plan Formulation', () => {
    it('creates a multi-step plan with sequential step numbering and status PENDING', () => {
      const plan = actionPlannerService.createPlan('Test Research Goal', [
        {
          skill: 'browser',
          action: 'navigate',
          description: 'Navigate to target site',
          arguments: { url: 'https://example.com' },
          risk_level: 'LOW_RISK',
        },
        {
          skill: 'browser',
          action: 'inspect_dom',
          description: 'Inspect structure',
          arguments: {},
          risk_level: 'READ_ONLY',
        },
      ]);

      expect(plan.plan_id).toBeDefined();
      expect(plan.steps.length).toBe(2);
      expect(plan.steps[0].step_number).toBe(1);
      expect(plan.steps[0].status).toBe('PENDING');
      expect(plan.steps[1].step_number).toBe(2);
      expect(plan.status).toBe('PLANNING');
    });

    it('synthesizes deterministic workflows for market research and UI inspection', () => {
      const researchPlan = actionPlannerService.createDeterministicWorkflow('market_research', { symbol: 'NVDA' });
      expect(researchPlan.goal).toContain('NVDA');
      expect(researchPlan.steps.length).toBe(3);
      expect(researchPlan.steps[0].skill).toBe('browser');
      expect(researchPlan.steps[2].skill).toBe('obsidian');

      const uiPlan = actionPlannerService.createDeterministicWorkflow('inspect_ui', {});
      expect(uiPlan.steps.length).toBe(3);
      expect(uiPlan.steps[0].action).toBe('get_monitors');
      expect(uiPlan.steps[1].action).toBe('capture_active_window');
    });
  });

  describe('Execution defers entirely to the backend Action Runtime', () => {
    it('executes steps by submitting each to actionRuntimeClient, never running them locally', async () => {
      const submitSpy = vi
        .spyOn(actionRuntimeClient, 'submitAction')
        .mockResolvedValue(result({ status: 'SUCCEEDED', summary: 'done' }));

      actionPlannerService.createPlan('Low Risk Navigation', [
        {
          skill: 'browser',
          action: 'navigate',
          description: 'Navigate to site',
          arguments: { url: 'https://tradingview.com' },
          risk_level: 'LOW_RISK',
        },
        {
          skill: 'browser',
          action: 'inspect_dom',
          description: 'Inspect DOM',
          arguments: {},
          risk_level: 'READ_ONLY',
        },
      ]);

      const res = await actionPlannerService.executePlan();
      expect(res.status).toBe('SUCCEEDED');
      expect(submitSpy).toHaveBeenCalledTimes(2);

      const plan = actionPlannerService.getActivePlan();
      expect(plan?.status).toBe('COMPLETED');
      expect(plan?.steps[0].status).toBe('SUCCEEDED');
      expect(plan?.steps[1].status).toBe('SUCCEEDED');
    });

    it('pauses only when the BACKEND reports CONFIRMATION_REQUIRED, ignoring a client-set risk_level', async () => {
      // The step is locally tagged CONFIRM_REQUIRED, but the backend is mocked
      // to approve it outright (SUCCEEDED). If the planner still had local
      // authority it would incorrectly halt here; it must not.
      const submitSpy = vi
        .spyOn(actionRuntimeClient, 'submitAction')
        .mockResolvedValue(result({ status: 'SUCCEEDED', summary: 'executed without prompting' }));

      actionPlannerService.createPlan('Client hint should not gate anything', [
        {
          skill: 'browser',
          action: 'click',
          description: 'Click something the client thinks is risky',
          arguments: { selector: '#go' },
          risk_level: 'CONFIRM_REQUIRED',
        },
      ]);

      const res = await actionPlannerService.executePlan();
      expect(res.status).toBe('SUCCEEDED');
      expect(actionPlannerService.getActivePlan()?.status).toBe('COMPLETED');
      expect(submitSpy).toHaveBeenCalledTimes(1);
    });

    it('pauses execution when the backend reports CONFIRMATION_REQUIRED and resumes via the real confirm endpoint', async () => {
      vi.spyOn(actionRuntimeClient, 'submitAction')
        .mockResolvedValueOnce(result({ status: 'SUCCEEDED', summary: 'opened' }))
        .mockResolvedValueOnce(
          result({
            status: 'CONFIRMATION_REQUIRED',
            summary: 'Needs confirmation',
            data: { confirmation_token: 'tok_123' },
          })
        );
      const confirmSpy = vi
        .spyOn(actionRuntimeClient, 'respondToConfirmation')
        .mockResolvedValue(result({ status: 'SUCCEEDED', summary: 'confirmed and executed' }));

      actionPlannerService.createPlan('Gated Trading Workflow', [
        {
          skill: 'browser',
          action: 'navigate',
          description: 'Open market',
          arguments: { url: 'https://tradingview.com' },
          risk_level: 'LOW_RISK',
        },
        {
          skill: 'browser',
          action: 'click',
          description: 'Click Order Execution Button',
          arguments: { selector: '#buy-order' },
          risk_level: 'LOW_RISK', // intentionally wrong -- backend decides, not this
        },
      ]);

      const firstRes = await actionPlannerService.executePlan();
      expect(firstRes.status).toBe('CONFIRMATION_REQUIRED');
      expect(actionPlannerService.getActivePlan()?.status).toBe('AWAITING_CONFIRMATION');

      const resumeRes = await actionPlannerService.resumeAfterConfirmation(true);
      expect(confirmSpy).toHaveBeenCalledWith(expect.any(String), 'tok_123', true);
      expect(resumeRes.status).toBe('SUCCEEDED');
      expect(actionPlannerService.getActivePlan()?.status).toBe('COMPLETED');
    });

    it('cancels the plan when the user denies confirmation, via the real confirm endpoint', async () => {
      vi.spyOn(actionRuntimeClient, 'submitAction').mockResolvedValue(
        result({
          status: 'CONFIRMATION_REQUIRED',
          summary: 'Needs confirmation',
          data: { confirmation_token: 'tok_456' },
        })
      );
      const confirmSpy = vi
        .spyOn(actionRuntimeClient, 'respondToConfirmation')
        .mockResolvedValue(result({ status: 'DENIED', summary: 'denied by user', error: 'User denied' }));

      actionPlannerService.createPlan('Denied Workflow', [
        {
          skill: 'browser',
          action: 'click',
          description: 'High Risk Action',
          arguments: { selector: '#delete-all' },
          risk_level: 'LOW_RISK',
        },
      ]);

      await actionPlannerService.executePlan();
      const denyRes = await actionPlannerService.resumeAfterConfirmation(false);
      expect(confirmSpy).toHaveBeenCalledWith(expect.any(String), 'tok_456', false);
      expect(denyRes.status).toBe('DENIED');
      expect(actionPlannerService.getActivePlan()?.status).toBe('CANCELLED');
    });

    it('fails closed if the backend requests confirmation without a confirmation_token', async () => {
      vi.spyOn(actionRuntimeClient, 'submitAction').mockResolvedValue(
        result({ status: 'CONFIRMATION_REQUIRED', summary: 'no token', data: {} })
      );

      actionPlannerService.createPlan('Malformed backend response', [
        {
          skill: 'browser',
          action: 'click',
          description: 'x',
          arguments: {},
          risk_level: 'LOW_RISK',
        },
      ]);

      const res = await actionPlannerService.executePlan();
      expect(res.status).toBe('CONFIRMATION_REQUIRED');
      expect(actionPlannerService.getActivePlan()?.status).toBe('FAILED');
    });

    it('halts the plan when the backend reports a step FAILED', async () => {
      const submitSpy = vi
        .spyOn(actionRuntimeClient, 'submitAction')
        .mockResolvedValue(result({ status: 'FAILED', summary: 'boom', error: 'element not found' }));

      actionPlannerService.createPlan('Failing plan', [
        {
          skill: 'browser',
          action: 'click',
          description: 'x',
          arguments: {},
          risk_level: 'LOW_RISK',
        },
      ]);

      const res = await actionPlannerService.executePlan();
      expect(res.status).toBe('FAILED');
      expect(actionPlannerService.getActivePlan()?.status).toBe('FAILED');
      expect(submitSpy).toHaveBeenCalledTimes(1);
    });
  });
});
