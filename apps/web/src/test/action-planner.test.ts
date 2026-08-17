import { describe, it, expect, beforeEach, vi } from 'vitest';
import { actionPlannerService } from '../services/action-planner';

describe('Wave 2B Multi-Step Action Planner & Execution Engine', () => {
  beforeEach(() => {
    actionPlannerService.clearPlan();
    vi.restoreAllMocks();
  });

  describe('Plan Formulation', () => {
    it('creates a multi-step plan with sequential step numbering and status PENDING', () => {
      const plan = actionPlannerService.createPlan('Test Research Goal', [
        {
          skill: 'browser',
          action: 'open_url',
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

  describe('Sequential Execution & Confirmation Gating', () => {
    it('executes low-risk steps sequentially to completion', async () => {
      actionPlannerService.createPlan('Low Risk Navigation', [
        {
          skill: 'browser',
          action: 'open_url',
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

      const plan = actionPlannerService.getActivePlan();
      expect(plan?.status).toBe('COMPLETED');
      expect(plan?.steps[0].status).toBe('SUCCEEDED');
      expect(plan?.steps[1].status).toBe('SUCCEEDED');
    });

    it('pauses execution when reaching a CONFIRM_REQUIRED step and resumes upon authorization', async () => {
      actionPlannerService.createPlan('Gated Trading Workflow', [
        {
          skill: 'browser',
          action: 'open_url',
          description: 'Open market',
          arguments: { url: 'https://tradingview.com' },
          risk_level: 'LOW_RISK',
        },
        {
          skill: 'browser',
          action: 'click',
          description: 'Click Order Execution Button',
          arguments: { selector: '#buy-order' },
          risk_level: 'CONFIRM_REQUIRED',
        },
      ]);

      // First execution pauses at step 2
      const firstRes = await actionPlannerService.executePlan();
      expect(firstRes.status).toBe('CONFIRMATION_REQUIRED');
      expect(actionPlannerService.getActivePlan()?.status).toBe('AWAITING_CONFIRMATION');

      // User authorizes the step
      const resumeRes = await actionPlannerService.resumeAfterConfirmation(true);
      expect(resumeRes.status).toBe('SUCCEEDED');
      expect(actionPlannerService.getActivePlan()?.status).toBe('COMPLETED');
    });

    it('cancels remaining steps when user denies confirmation', async () => {
      actionPlannerService.createPlan('Denied Workflow', [
        {
          skill: 'browser',
          action: 'click',
          description: 'High Risk Action',
          arguments: { selector: '#delete-all' },
          risk_level: 'CONFIRM_REQUIRED',
        },
      ]);

      await actionPlannerService.executePlan();
      const denyRes = await actionPlannerService.resumeAfterConfirmation(false);
      expect(denyRes.status).toBe('DENIED');
      expect(actionPlannerService.getActivePlan()?.status).toBe('CANCELLED');
    });
  });
});
