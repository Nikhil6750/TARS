import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ActiveContextBar } from '../components/hud/ActiveContextBar';
import { ActionConfirmationCard } from '../components/hud/ActionConfirmationCard';
import { ActionResultView } from '../components/hud/ActionResultView';
import { HUDOverlay } from '../components/hud/HUDOverlay';
import { ActionRequest, ActionResult, ActiveWindowContext } from '../types/actions';

describe('Wave 2A HUD UI Components', () => {
  it('renders ActiveContextBar with executable and window title', () => {
    const ctx: ActiveWindowContext = {
      executable: 'notepad.exe',
      process_id: 1234,
      window_title: 'trading_plan.txt - Notepad',
      window_bounds: { x: 50, y: 50, width: 800, height: 600 },
      captured_at: new Date().toISOString(),
    };

    const onRefresh = vi.fn();
    render(<ActiveContextBar activeContext={ctx} onRefresh={onRefresh} />);

    expect(screen.getByText('notepad.exe')).toBeDefined();
    expect(screen.getByText('trading_plan.txt - Notepad')).toBeDefined();

    const refreshBtn = screen.getByTitle('Refresh Active Window Snapshot');
    fireEvent.click(refreshBtn);
    expect(onRefresh).toHaveBeenCalledOnce();
  });

  it('renders ActionConfirmationCard with exact command and user confirmation buttons', () => {
    const req: ActionRequest = {
      schema_version: '1.0.0',
      id: 'req_12345',
      skill: 'terminal',
      action: 'run_command',
      arguments: { command: 'git pull origin main' },
      source: 'hud',
      requested_at: new Date().toISOString(),
    };

    const onConfirm = vi.fn();
    const onDeny = vi.fn();

    render(
      <ActionConfirmationCard
        request={req}
        onConfirm={onConfirm}
        onDeny={onDeny}
      />
    );

    expect(screen.getByText('CONFIRM_REQUIRED')).toBeDefined();
    expect(screen.getByText('terminal.run_command()')).toBeDefined();
    expect(screen.getByText('git pull origin main')).toBeDefined();

    const confirmBtn = screen.getByText('CONFIRM & EXECUTE');
    fireEvent.click(confirmBtn);
    expect(onConfirm).toHaveBeenCalledWith('req_12345');

    const denyBtn = screen.getByText('DENY');
    fireEvent.click(denyBtn);
    // Opens deny reason input
    const submitDenyBtn = screen.getByText('SUBMIT DENIAL');
    fireEvent.click(submitDenyBtn);
    expect(onDeny).toHaveBeenCalledWith('req_12345', undefined);
  });

  it('renders ActionResultView strictly for SUCCEEDED, FAILED, and BLOCKED results', () => {
    const succeeded: ActionResult = {
      schema_version: '1.0.0',
      request_id: 'req_5555',
      status: 'SUCCEEDED',
      risk_level: 'LOW_RISK',
      summary: 'Opened URL in default browser: https://tradingview.com',
      data: { url: 'https://tradingview.com' },
      error: null,
      started_at: new Date().toISOString(),
      completed_at: new Date().toISOString(),
    };

    const { rerender } = render(<ActionResultView result={succeeded} />);
    expect(screen.getByText('SUCCEEDED')).toBeDefined();
    expect(screen.getByText('LOW_RISK')).toBeDefined();
    expect(screen.getByText('Opened URL in default browser: https://tradingview.com')).toBeDefined();

    const blocked: ActionResult = {
      schema_version: '1.0.0',
      request_id: 'req_6666',
      status: 'BLOCKED',
      risk_level: 'BLOCKED',
      summary: 'Command blocked by security policy: "format C:"',
      data: {},
      error: 'Destructive operations are permanently blocked.',
      started_at: new Date().toISOString(),
      completed_at: new Date().toISOString(),
    };

    rerender(<ActionResultView result={blocked} />);
    expect(screen.getAllByText('BLOCKED').length).toBeGreaterThan(0);
    expect(screen.getByText('Destructive operations are permanently blocked.')).toBeDefined();
  });

  it('renders HUDOverlay with interactive command input and PTT controls', () => {
    const onExpand = vi.fn();
    const onTogglePtt = vi.fn();

    render(
      <HUDOverlay
        companionState="IDLE"
        onExpand={onExpand}
        activeSetups={[]}
        criticalWarnings={[]}
        isListening={false}
        onTogglePushToTalk={onTogglePtt}
        audioVolume={0}
      />
    );

    expect(screen.getByText('TARS HUD')).toBeDefined();
    expect(screen.getByText('WAVE 2A')).toBeDefined();
    expect(screen.getByPlaceholderText(/Run action or ask TARS/)).toBeDefined();

    const expandBtn = screen.getByTitle('Expand to Full Workstation');
    fireEvent.click(expandBtn);
    expect(onExpand).toHaveBeenCalledOnce();
  });
});
