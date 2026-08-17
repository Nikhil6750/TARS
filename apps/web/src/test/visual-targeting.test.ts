import { describe, it, expect } from 'vitest';
import { visualTargetingService } from '../services/visual-targeting';
import { DOMElementSummary, MonitorInfo, UIElementNode } from '../types/actions';

describe('Wave 2B Visual & Semantic Targeting Service', () => {
  const sampleDOMTree: DOMElementSummary[] = [
    {
      id: 'search-input',
      selector: 'input#search-input',
      tag: 'input',
      role: 'textbox',
      text: '',
      placeholder: 'Search trading instruments...',
      type: 'text',
      bounds: { x: 100, y: 50, width: 300, height: 35 },
      is_interactive: true,
      is_sensitive: false,
      is_visible: true,
      attributes: { id: 'search-input', placeholder: 'Search trading instruments...' },
    },
    {
      id: 'password-input',
      selector: 'input#password-input',
      tag: 'input',
      role: 'textbox',
      text: '',
      placeholder: 'Enter API Secret Password',
      type: 'password',
      bounds: { x: 100, y: 100, width: 300, height: 35 },
      is_interactive: true,
      is_sensitive: true,
      is_visible: true,
      attributes: { id: 'password-input', type: 'password' },
    },
    {
      id: 'submit-order-btn',
      selector: 'button#submit-order-btn',
      tag: 'button',
      role: 'button',
      text: 'Submit Order Confirmation',
      bounds: { x: 100, y: 160, width: 180, height: 40 },
      is_interactive: true,
      is_sensitive: false,
      is_visible: true,
      attributes: { id: 'submit-order-btn', role: 'button' },
    },
  ];

  const sampleUITree: UIElementNode = {
    id: 'hwnd_root',
    name: 'Notepad',
    role: 'window',
    class_name: 'Notepad',
    bounds: { x: 50, y: 50, width: 800, height: 600 },
    is_enabled: true,
    is_visible: true,
    children: [
      {
        id: 'hwnd_edit_1',
        name: 'Text Editor',
        role: 'input',
        class_name: 'Edit',
        bounds: { x: 60, y: 80, width: 780, height: 500 },
        is_enabled: true,
        is_visible: true,
        children: [],
      },
    ],
  };

  const sampleMonitors: MonitorInfo[] = [
    {
      id: 'DISPLAY_PRIMARY',
      name: 'Primary Display',
      is_primary: true,
      bounds: { x: 0, y: 0, width: 1920, height: 1080 },
      work_area: { x: 0, y: 0, width: 1920, height: 1040 },
      scale_factor: 1.0,
      dpi: 96,
    },
  ];

  describe('Preference Hierarchy: Semantic DOM Target', () => {
    it('prioritizes semantic DOM matching over visual coordinates', () => {
      const res = visualTargetingService.resolveTarget(
        { query: 'Search trading instruments' },
        { domTree: sampleDOMTree, uiTree: sampleUITree, monitors: sampleMonitors }
      );

      expect(res.target_type).toBe('semantic_dom');
      expect(res.element).toBeDefined();
      expect((res.element as DOMElementSummary).id).toBe('search-input');
      expect(res.proposed_action.skill).toBe('browser');
      expect(res.proposed_action.action).toBe('type');
      expect(res.coordinates).toEqual({ x: 250, y: 67.5 });
    });

    it('identifies state-changing buttons and classifies risk as CONFIRM_REQUIRED', () => {
      const res = visualTargetingService.resolveTarget(
        { query: 'Submit Order Confirmation' },
        { domTree: sampleDOMTree, uiTree: sampleUITree, monitors: sampleMonitors }
      );

      expect(res.target_type).toBe('semantic_dom');
      expect(res.proposed_action.action).toBe('click');
      expect(res.proposed_action.risk_level).toBe('CONFIRM_REQUIRED');
    });
  });

  describe('Preference Hierarchy: Native Accessibility Target', () => {
    it('resolves native Win32 accessibility element when DOM is absent', () => {
      const res = visualTargetingService.resolveTarget(
        { query: 'Text Editor' },
        { domTree: [], uiTree: sampleUITree, monitors: sampleMonitors }
      );

      expect(res.target_type).toBe('accessibility_element');
      expect(res.element).toBeDefined();
      expect(res.proposed_action.skill).toBe('windows_app');
      expect(res.proposed_action.action).toBe('send_keys');
    });
  });

  describe('Preference Hierarchy: Visual Coordinates Fallback', () => {
    it('falls back to visual coordinate if no semantic target matches and coordinate is provided', () => {
      const res = visualTargetingService.resolveTarget(
        { query: 'coordinate click', coordinate_hint: { x: 500, y: 400 } },
        { domTree: [], uiTree: undefined, monitors: sampleMonitors }
      );

      expect(res.target_type).toBe('visual_coordinate');
      expect(res.coordinates).toEqual({ x: 500, y: 400 });
      expect(res.proposed_action.skill).toBe('windows_app');
      expect(res.proposed_action.action).toBe('click_coordinate');
    });

    it('rejects out-of-bounds coordinates cleanly without random clicks', () => {
      const res = visualTargetingService.resolveTarget(
        { query: 'out of bounds click', coordinate_hint: { x: 5000, y: 9000 } },
        { domTree: [], uiTree: undefined, monitors: sampleMonitors }
      );

      expect(res.target_type).toBe('unresolved');
      expect(res.proposed_action.risk_level).toBe('BLOCKED');
    });
  });

  describe('Sensitive Field Protection', () => {
    it('flags password and secret inputs as sensitive and redacts values', () => {
      const isSens = visualTargetingService.isSensitiveElement(
        { type: 'password', name: 'api_secret' },
        'Secret Key',
        'input',
        'password'
      );
      expect(isSens).toBe(true);

      const sanitized = visualTargetingService.sanitizeSensitiveValue('super_secret_123', true);
      expect(sanitized).toBe('[REDACTED_SENSITIVE]');
    });
  });

  describe('ActionRequest Synthesis', () => {
    it('converts a TargetingResolution into a valid ActionRequest', () => {
      const res = visualTargetingService.resolveTarget(
        { query: 'Search trading instruments' },
        { domTree: sampleDOMTree, monitors: sampleMonitors }
      );

      const req = visualTargetingService.createActionFromTarget(res);
      expect(req.schema_version).toBe('1.0.0');
      expect(req.skill).toBe('browser');
      expect(req.action).toBe('type');
      expect(req.id).toBeDefined();
    });
  });
});
