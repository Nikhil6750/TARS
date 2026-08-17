import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { VisualInspectorCard } from '../components/hud/VisualInspectorCard';
import { BrowserContextCard } from '../components/hud/BrowserContextCard';
import { MultiStepPlanView } from '../components/hud/MultiStepPlanView';
import { HUDOverlay } from '../components/hud/HUDOverlay';
import { ScreenCaptureResult, UIElementNode, MonitorInfo, BrowserPageContext, MultiStepActionPlan } from '../types/actions';

describe('Wave 2B HUD UI & Context Components', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  const mockCapture: ScreenCaptureResult = {
    capture_id: 'cap_test_123',
    captured_at: new Date().toISOString(),
    source: 'active_window',
    executable: 'chrome.exe',
    window_title: 'TradingView — BTCUSDT Chart',
    bounds: { x: 0, y: 0, width: 1920, height: 1080 },
    scale_factor: 1.0,
    dpi: 96,
    width: 1920,
    height: 1080,
    is_secure_desktop: false,
    image_format: 'image/bmp',
    image_data_base64: 'data:image/bmp;base64,Qk0AAAAAAAAAAAAAAA==',
    temp_file_path: null,
    error: null,
  };

  const mockUITree: UIElementNode = {
    id: 'hwnd_win',
    name: 'TradingView',
    role: 'window',
    class_name: 'Chrome_WidgetWin_1',
    bounds: { x: 0, y: 0, width: 1920, height: 1080 },
    is_enabled: true,
    is_visible: true,
    children: [
      {
        id: 'hwnd_btn_1',
        name: 'Buy Market',
        role: 'button',
        class_name: 'Button',
        bounds: { x: 100, y: 100, width: 120, height: 40 },
        is_enabled: true,
        is_visible: true,
        children: [],
      },
    ],
  };

  const mockMonitors: MonitorInfo[] = [
    {
      id: 'DISPLAY1',
      name: 'Primary 4K Monitor',
      is_primary: true,
      bounds: { x: 0, y: 0, width: 3840, height: 2160 },
      work_area: { x: 0, y: 0, width: 3840, height: 2120 },
      scale_factor: 1.5,
      dpi: 144,
    },
  ];

  const mockBrowserContext: BrowserPageContext = {
    url: 'https://tradingview.com/chart/BTCUSDT',
    title: 'BTC/USDT Chart & Order Book',
    is_loading: false,
    can_go_back: true,
    can_go_forward: false,
    headings: ['Market Overview', 'Order Book'],
    links_count: 5,
    inputs_count: 2,
    buttons_count: 4,
    dom_tree: [
      {
        id: 'buy-btn',
        selector: 'button#buy-btn',
        tag: 'button',
        role: 'button',
        text: 'Buy BTC',
        is_interactive: true,
        is_sensitive: false,
        is_visible: true,
        attributes: { id: 'buy-btn' },
      },
    ],
    captured_at: new Date().toISOString(),
  };

  describe('VisualInspectorCard', () => {
    it('renders visual snapshot, DPI scale factor, and tabs', () => {
      render(
        <VisualInspectorCard
          capture={mockCapture}
          uiTree={mockUITree}
          monitors={mockMonitors}
          onRefreshCapture={vi.fn()}
        />
      );

      expect(screen.getByText(/WHAT TARS SEES/i)).toBeInTheDocument();
      expect(screen.getByText(/DPI: 96/i)).toBeInTheDocument();
      expect(screen.getByText(/Snapshot/i)).toBeInTheDocument();
      expect(screen.getByText(/UI Tree/i)).toBeInTheDocument();
      expect(screen.getByText(/Monitors/i)).toBeInTheDocument();
    });

    it('renders secure desktop warning when secure desktop is active', () => {
      const secureCapture: ScreenCaptureResult = {
        ...mockCapture,
        is_secure_desktop: true,
        image_data_base64: null,
      };

      render(
        <VisualInspectorCard
          capture={secureCapture}
          uiTree={mockUITree}
          monitors={mockMonitors}
          onRefreshCapture={vi.fn()}
        />
      );

      expect(screen.getByText(/SECURE DESKTOP ACTIVE/i)).toBeInTheDocument();
      expect(screen.getByText(/Screen capture prohibited/i)).toBeInTheDocument();
    });
  });

  describe('BrowserContextCard', () => {
    it('renders live browser URL, navigation controls, and DOM elements', () => {
      const onNav = vi.fn();
      render(
        <BrowserContextCard
          context={mockBrowserContext}
          onNavigate={onNav}
        />
      );

      expect(screen.getByDisplayValue('https://tradingview.com/chart/BTCUSDT')).toBeInTheDocument();
      expect(screen.getByText(/DOM \(1\)/i)).toBeInTheDocument();
      expect(screen.getByText(/Buy BTC/i)).toBeInTheDocument();
    });
  });

  describe('MultiStepPlanView', () => {
    it('renders multi-step sequence, step numbering, and execution trigger', () => {
      const mockPlan: MultiStepActionPlan = {
        plan_id: 'plan_1',
        goal: 'Analyze NVDA breakout',
        status: 'PLANNING',
        current_step_index: 0,
        created_at: new Date().toISOString(),
        steps: [
          {
            step_number: 1,
            skill: 'browser',
            action: 'open_url',
            description: 'Open NVDA chart',
            arguments: {},
            risk_level: 'LOW_RISK',
            status: 'PENDING',
          },
          {
            step_number: 2,
            skill: 'browser',
            action: 'inspect_dom',
            description: 'Inspect structure',
            arguments: {},
            risk_level: 'READ_ONLY',
            status: 'PENDING',
          },
        ],
      };

      const onExec = vi.fn();
      render(
        <MultiStepPlanView
          plan={mockPlan}
          onExecute={onExec}
          onResume={vi.fn()}
          onCancel={vi.fn()}
        />
      );

      expect(screen.getByText(/PLAN: Analyze NVDA breakout/i)).toBeInTheDocument();
      expect(screen.getByText(/Open NVDA chart/i)).toBeInTheDocument();
      expect(screen.getByText(/RUN 2-STEP PLAN/i)).toBeInTheDocument();

      fireEvent.click(screen.getByText(/RUN 2-STEP PLAN/i));
      expect(onExec).toHaveBeenCalled();
    });
  });

  describe('HUDOverlay Integration', () => {
    it('renders HUDOverlay with Wave 2B badge and quick action pills', async () => {
      await act(async () => {
        render(
          <HUDOverlay
            companionState="IDLE"
            onExpand={vi.fn()}
            activeSetups={[]}
            criticalWarnings={[]}
            isListening={false}
            onTogglePushToTalk={vi.fn()}
            audioVolume={0}
          />
        );
      });

      expect(screen.getByText(/WAVE 2B/i)).toBeInTheDocument();
      expect(screen.getByText(/Capture/i)).toBeInTheDocument();
      expect(screen.getByText(/DOM/i)).toBeInTheDocument();
      expect(screen.getByText(/Workflow/i)).toBeInTheDocument();
      expect(screen.getByPlaceholderText(/Run action, target element, or ask TARS/i)).toBeInTheDocument();
    });
  });
});
