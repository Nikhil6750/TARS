/**
 * Wave 2B Visual & Semantic Targeting Service
 * Maps visual/UI queries -> semantic accessibility elements or screen coordinates -> schema-compliant ActionRequest.
 * 
 * Preference hierarchy (Strict):
 * 1. Semantic accessibility / DOM target (name, role, label, selector, id, text).
 * 2. Visual coordinates ONLY if semantic target is unavailable or coordinate-explicit.
 * 
 * Safety:
 * - Coordinates validated against monitor geometry / window bounds (no blind off-screen clicks).
 * - Sensitive fields (password, payment, credit card, auth token) identified and values redacted from logs/prompts.
 * - No random click loops.
 */

import {
  ActionRequest,
  ActiveWindowContext,
  DOMElementSummary,
  MonitorInfo,
  RiskLevel,
  SemanticCriteria,
  TargetingResolution,
  UIElementNode,
  VisualTargetQuery,
  WindowBounds,
} from '../types/actions';
import { actionRuntimeClient } from './actions';

const SENSITIVE_KEYWORDS = [
  'password',
  'passwd',
  'secret',
  'token',
  'api_key',
  'apikey',
  'auth',
  'card',
  'cc-number',
  'cvv',
  'cvc',
  'ssn',
  'security code',
  'pin',
];

export class VisualTargetingService {
  /**
   * Identifies whether an input or element represents sensitive confidential information.
   */
  public isSensitiveElement(
    attrs: Record<string, string>,
    name: string = '',
    tag: string = '',
    type: string = ''
  ): boolean {
    const text = `${name} ${tag} ${type} ${Object.values(attrs).join(' ')}`.toLowerCase();
    return SENSITIVE_KEYWORDS.some((kw) => text.includes(kw));
  }

  /**
   * Sanitizes input text if directed at a sensitive target.
   */
  public sanitizeSensitiveValue(value: string, isSensitive: boolean): string {
    if (!isSensitive) return value;
    return '[REDACTED_SENSITIVE]';
  }

  /**
   * Validates coordinate against screen and window boundaries.
   */
  public validateCoordinates(
    x: number,
    y: number,
    monitors: MonitorInfo[],
    windowBounds?: WindowBounds | null
  ): { valid: boolean; reason?: string } {
    if (isNaN(x) || isNaN(y)) {
      return { valid: false, reason: 'Coordinates are NaN' };
    }

    if (windowBounds) {
      const inWindowX = x >= windowBounds.x && x <= windowBounds.x + windowBounds.width;
      const inWindowY = y >= windowBounds.y && y <= windowBounds.y + windowBounds.height;
      if (!inWindowX || !inWindowY) {
        return {
          valid: false,
          reason: `Coordinates (${x}, ${y}) fall outside active window bounds (${windowBounds.x}, ${windowBounds.y}, ${windowBounds.width}x${windowBounds.height})`,
        };
      }
      return { valid: true };
    }

    // Check if within any monitor bounds
    const insideAny = monitors.some((m) => {
      return (
        x >= m.bounds.x &&
        x <= m.bounds.x + m.bounds.width &&
        y >= m.bounds.y &&
        y <= m.bounds.y + m.bounds.height
      );
    });

    if (!insideAny && monitors.length > 0) {
      return {
        valid: false,
        reason: `Coordinates (${x}, ${y}) fall outside all connected monitor boundaries.`,
      };
    }

    return { valid: true };
  }

  /**
   * Resolves a targeting query against available DOM elements or Win32 UI elements.
   */
  public resolveTarget(
    query: VisualTargetQuery,
    context: {
      domTree?: DOMElementSummary[];
      uiTree?: UIElementNode;
      monitors?: MonitorInfo[];
      windowBounds?: WindowBounds | null;
      activeExecutable?: string;
    }
  ): TargetingResolution {
    const qLower = query.query.toLowerCase().trim();
    const criteria: SemanticCriteria = query.semantic_criteria || {};

    // 1. Check DOM tree first (highest semantic fidelity for web/browser apps)
    if (context.domTree && context.domTree.length > 0) {
      const match = this.findMatchingDOMElement(context.domTree, qLower, criteria);
      if (match) {
        const isSensitive = match.is_sensitive;
        const proposedAction = this.buildProposedAction(
          match,
          'browser',
          qLower,
          isSensitive
        );

        return {
          target_type: 'semantic_dom',
          element: match,
          coordinates: match.bounds ? { x: match.bounds.x + match.bounds.width / 2, y: match.bounds.y + match.bounds.height / 2 } : null,
          proposed_action: proposedAction,
          confidence_rationale: `Resolved semantic DOM element: <${match.tag}> "${match.text || match.placeholder || match.selector}" (Role: ${match.role || 'generic'})`,
        };
      }
    }

    // 2. Check Win32 native accessibility / UI element tree
    if (context.uiTree && context.uiTree.children.length > 0) {
      const match = this.findMatchingUIElement(context.uiTree, qLower, criteria);
      if (match) {
        const proposedAction = this.buildProposedUIAction(match, context.activeExecutable || 'windows_app', qLower);
        return {
          target_type: 'accessibility_element',
          element: match,
          coordinates: match.bounds ? { x: match.bounds.x + match.bounds.width / 2, y: match.bounds.y + match.bounds.height / 2 } : null,
          proposed_action: proposedAction,
          confidence_rationale: `Resolved native accessibility element: "${match.name}" (Role: ${match.role}, Class: ${match.class_name})`,
        };
      }
    }

    // 3. Fallback to explicit visual coordinate ONLY if provided and valid
    if (query.coordinate_hint) {
      const { x, y } = query.coordinate_hint;
      const validation = this.validateCoordinates(
        x,
        y,
        context.monitors || [],
        context.windowBounds
      );

      if (validation.valid) {
        return {
          target_type: 'visual_coordinate',
          coordinates: { x, y },
          proposed_action: {
            skill: 'windows_app',
            action: 'click_coordinate',
            arguments: { x, y },
            risk_level: 'CONFIRM_REQUIRED',
            description: `Click screen coordinate (${x}, ${y})`,
          },
          confidence_rationale: `Direct visual coordinate target (${x}, ${y}) within valid boundaries.`,
        };
      } else {
        return {
          target_type: 'unresolved',
          coordinates: null,
          proposed_action: {
            skill: 'windows_app',
            action: 'noop',
            arguments: { error: validation.reason },
            risk_level: 'BLOCKED',
            description: `Targeting rejected: ${validation.reason}`,
          },
          confidence_rationale: `Coordinate (${x}, ${y}) invalid: ${validation.reason}`,
        };
      }
    }

    // 4. Unresolved target
    return {
      target_type: 'unresolved',
      element: null,
      coordinates: null,
      proposed_action: {
        skill: 'windows_app',
        action: 'noop',
        arguments: { query: query.query },
        risk_level: 'READ_ONLY',
        description: `No matching semantic element or coordinate found for "${query.query}"`,
      },
      confidence_rationale: `Could not identify interactive target matching "${query.query}". No random clicks executed.`,
    };
  }

  /**
   * Converts a TargetingResolution into a validated ActionRequest.
   */
  public createActionFromTarget(
    resolution: TargetingResolution,
    activeContext?: ActiveWindowContext | null
  ): ActionRequest {
    const { proposed_action } = resolution;
    return actionRuntimeClient.createRequest({
      skill: proposed_action.skill,
      action: proposed_action.action,
      arguments: proposed_action.arguments,
      source: 'hud',
      activeContext,
    });
  }

  private findMatchingDOMElement(
    domTree: DOMElementSummary[],
    query: string,
    criteria: SemanticCriteria
  ): DOMElementSummary | null {
    // Exact selector / id match
    if (criteria.selector) {
      const found = domTree.find((el) => el.selector === criteria.selector);
      if (found) return found;
    }
    if (criteria.id) {
      const found = domTree.find((el) => el.id === criteria.id);
      if (found) return found;
    }

    // Exact text / aria-label / placeholder match
    for (const el of domTree) {
      const textMatch = el.text && el.text.toLowerCase() === query;
      const placeholderMatch = el.placeholder && el.placeholder.toLowerCase() === query;
      const ariaMatch = el.attributes['aria-label'] && el.attributes['aria-label'].toLowerCase() === query;
      if (textMatch || placeholderMatch || ariaMatch) {
        return el;
      }
    }

    // Substring / keyword match (prefer interactive elements like buttons/inputs)
    const candidates = domTree.filter((el) => {
      const text = `${el.text} ${el.placeholder || ''} ${el.attributes['aria-label'] || ''} ${el.attributes['name'] || ''}`.toLowerCase();
      return text.includes(query);
    });

    if (candidates.length > 0) {
      // Prioritize interactive button or input over generic text
      const interactive = candidates.find((el) => el.is_interactive);
      return interactive || candidates[0];
    }

    return null;
  }

  private findMatchingUIElement(
    root: UIElementNode,
    query: string,
    criteria: SemanticCriteria
  ): UIElementNode | null {
    const queue: UIElementNode[] = [root, ...root.children];

    while (queue.length > 0) {
      const current = queue.shift()!;
      const name = current.name.toLowerCase();
      const role = current.role.toLowerCase();

      if (criteria.role && role === criteria.role.toLowerCase()) {
        if (!query || name.includes(query)) return current;
      }

      if (name && (name === query || name.includes(query))) {
        return current;
      }

      for (const child of current.children) {
        queue.push(child);
      }
    }

    return null;
  }

  private buildProposedAction(
    el: DOMElementSummary,
    skill: string,
    _query: string,
    isSensitive: boolean
  ): { skill: string; action: string; arguments: Record<string, unknown>; risk_level: RiskLevel; description: string } {
    if (el.tag === 'input' || el.tag === 'textarea') {
      const isSubmit = el.type === 'submit' || el.type === 'button';
      if (isSubmit) {
        return {
          skill,
          action: 'click',
          arguments: { selector: el.selector },
          risk_level: 'CONFIRM_REQUIRED',
          description: `Submit form via button <${el.tag}> "${el.text || el.selector}"`,
        };
      }
      return {
        skill,
        action: 'type',
        arguments: { selector: el.selector, is_sensitive: isSensitive },
        risk_level: 'LOW_RISK',
        description: `Focus/type into <${el.tag}> ${isSensitive ? '[SENSITIVE FIELD]' : el.placeholder || el.selector}`,
      };
    }

    if (el.tag === 'button' || el.role === 'button' || el.tag === 'a') {
      const isStateChange = /submit|buy|purchase|delete|confirm|pay/i.test(el.text);
      return {
        skill,
        action: 'click',
        arguments: { selector: el.selector },
        risk_level: isStateChange ? 'CONFIRM_REQUIRED' : 'LOW_RISK',
        description: `Click ${el.role || el.tag} "${el.text || el.selector}"`,
      };
    }

    return {
      skill,
      action: 'click',
      arguments: { selector: el.selector },
      risk_level: 'LOW_RISK',
      description: `Click element <${el.tag}> "${el.text || el.selector}"`,
    };
  }

  private buildProposedUIAction(
    el: UIElementNode,
    _skill: string,
    _query: string
  ): { skill: string; action: string; arguments: Record<string, unknown>; risk_level: RiskLevel; description: string } {
    if (el.role === 'input') {
      return {
        skill: 'windows_app',
        action: 'send_keys',
        arguments: { target_element: el.id, element_name: el.name },
        risk_level: 'LOW_RISK',
        description: `Type into UI control "${el.name || el.class_name}"`,
      };
    }

    return {
      skill: 'windows_app',
      action: 'click_element',
      arguments: { target_element: el.id, element_name: el.name, bounds: el.bounds },
      risk_level: 'LOW_RISK',
      description: `Click UI element "${el.name || el.class_name}" (${el.role})`,
    };
  }
}

export const visualTargetingService = new VisualTargetingService();
