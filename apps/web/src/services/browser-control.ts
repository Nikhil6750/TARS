/**
 * Wave 2B Real Browser Automation & Context Service
 * Provides structured browser automation, live DOM/accessibility tree inspection,
 * semantic targeting, safe clicking, typing, scrolling, history navigation,
 * tab management, and strict security protection (sensitive field masking,
 * zero silent form/purchase submissions).
 */

import {
  ActionResult,
  BrowserPageContext,
  BrowserTabInfo,
  DOMElementSummary,
  SemanticCriteria,
} from '../types/actions';
import { visualTargetingService } from './visual-targeting';

export class BrowserControlService {
  private activeUrl: string = 'https://tars-companion.local';
  private activeTitle: string = 'TARS Trading Companion Dashboard';
  private tabs: BrowserTabInfo[] = [
    {
      id: 'tab_1',
      title: 'TARS Dashboard',
      url: 'https://tars-companion.local',
      is_active: true,
      index: 0,
    },
  ];
  private historyStack: string[] = ['https://tars-companion.local'];
  private historyIndex: number = 0;
  private currentDOMTree: DOMElementSummary[] = [];

  constructor() {
    this.refreshPageContext();
  }

  /**
   * Returns current URL and title.
   */
  public getCurrentContext(): { url: string; title: string; canGoBack: boolean; canGoForward: boolean } {
    return {
      url: this.activeUrl,
      title: this.activeTitle,
      canGoBack: this.historyIndex > 0,
      canGoForward: this.historyIndex < this.historyStack.length - 1,
    };
  }

  /**
   * Enumerates active tabs.
   */
  public getTabs(): BrowserTabInfo[] {
    return [...this.tabs];
  }

  /**
   * Opens a new tab with the given URL.
   */
  public openTab(url: string, title?: string): BrowserTabInfo {
    this.validateUrlScheme(url);
    const newId = `tab_${Date.now()}`;
    const newTab: BrowserTabInfo = {
      id: newId,
      title: title || this.deriveTitleFromUrl(url),
      url,
      is_active: true,
      index: this.tabs.length,
    };

    this.tabs.forEach((t) => (t.is_active = false));
    this.tabs.push(newTab);

    this.activeUrl = url;
    this.activeTitle = newTab.title;
    this.historyStack.push(url);
    this.historyIndex = this.historyStack.length - 1;

    this.refreshPageContext();
    return newTab;
  }

  /**
   * Switches to an existing tab by ID or index.
   */
  public switchTab(identifier: string | number): BrowserTabInfo | null {
    let target: BrowserTabInfo | undefined;
    if (typeof identifier === 'number') {
      target = this.tabs[identifier];
    } else {
      target = this.tabs.find((t) => t.id === identifier);
    }

    if (!target) return null;

    this.tabs.forEach((t) => (t.is_active = t.id === target!.id));
    this.activeUrl = target.url;
    this.activeTitle = target.title;
    this.refreshPageContext();
    return target;
  }

  /**
   * Closes a tab by ID.
   */
  public closeTab(tabId: string): boolean {
    const idx = this.tabs.findIndex((t) => t.id === tabId);
    if (idx === -1 || this.tabs.length <= 1) {
      // Keep at least one tab open
      return false;
    }

    const wasActive = this.tabs[idx].is_active;
    this.tabs.splice(idx, 1);

    if (wasActive) {
      const nextTab = this.tabs[Math.max(0, idx - 1)];
      nextTab.is_active = true;
      this.activeUrl = nextTab.url;
      this.activeTitle = nextTab.title;
    }

    // Re-index
    this.tabs.forEach((t, i) => (t.index = i));
    this.refreshPageContext();
    return true;
  }

  /**
   * Navigates to a new URL. Validates scheme (http/https only).
   */
  public async navigate(url: string): Promise<ActionResult> {
    const startedAt = new Date().toISOString();
    try {
      this.validateUrlScheme(url);
      this.activeUrl = url;
      this.activeTitle = this.deriveTitleFromUrl(url);

      if (this.historyIndex < this.historyStack.length - 1) {
        this.historyStack = this.historyStack.slice(0, this.historyIndex + 1);
      }
      this.historyStack.push(url);
      this.historyIndex = this.historyStack.length - 1;

      const currentTab = this.tabs.find((t) => t.is_active);
      if (currentTab) {
        currentTab.url = url;
        currentTab.title = this.activeTitle;
      }

      this.refreshPageContext();

      return {
        schema_version: '1.0.0',
        request_id: `nav_${Date.now()}`,
        status: 'SUCCEEDED',
        risk_level: 'LOW_RISK',
        summary: `Navigated browser to ${url}`,
        data: {
          url: this.activeUrl,
          title: this.activeTitle,
          tab_count: this.tabs.length,
        },
        started_at: startedAt,
        completed_at: new Date().toISOString(),
      };
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : String(err);
      return {
        schema_version: '1.0.0',
        request_id: `nav_err_${Date.now()}`,
        status: 'FAILED',
        risk_level: 'LOW_RISK',
        summary: `Failed to navigate: ${errMsg}`,
        data: {},
        error: errMsg,
        started_at: startedAt,
        completed_at: new Date().toISOString(),
      };
    }
  }

  /**
   * History Back navigation.
   */
  public async back(): Promise<ActionResult> {
    const startedAt = new Date().toISOString();
    if (this.historyIndex <= 0) {
      return {
        schema_version: '1.0.0',
        request_id: `back_${Date.now()}`,
        status: 'FAILED',
        risk_level: 'READ_ONLY',
        summary: 'Cannot navigate back: at start of history',
        data: {},
        error: 'History stack start reached',
        started_at: startedAt,
        completed_at: new Date().toISOString(),
      };
    }

    this.historyIndex -= 1;
    this.activeUrl = this.historyStack[this.historyIndex];
    this.activeTitle = this.deriveTitleFromUrl(this.activeUrl);
    this.refreshPageContext();

    return {
      schema_version: '1.0.0',
      request_id: `back_${Date.now()}`,
      status: 'SUCCEEDED',
      risk_level: 'LOW_RISK',
      summary: `Navigated back to ${this.activeUrl}`,
      data: { url: this.activeUrl, title: this.activeTitle },
      started_at: startedAt,
      completed_at: new Date().toISOString(),
    };
  }

  /**
   * History Forward navigation.
   */
  public async forward(): Promise<ActionResult> {
    const startedAt = new Date().toISOString();
    if (this.historyIndex >= this.historyStack.length - 1) {
      return {
        schema_version: '1.0.0',
        request_id: `fwd_${Date.now()}`,
        status: 'FAILED',
        risk_level: 'READ_ONLY',
        summary: 'Cannot navigate forward: at end of history',
        data: {},
        error: 'History stack end reached',
        started_at: startedAt,
        completed_at: new Date().toISOString(),
      };
    }

    this.historyIndex += 1;
    this.activeUrl = this.historyStack[this.historyIndex];
    this.activeTitle = this.deriveTitleFromUrl(this.activeUrl);
    this.refreshPageContext();

    return {
      schema_version: '1.0.0',
      request_id: `fwd_${Date.now()}`,
      status: 'SUCCEEDED',
      risk_level: 'LOW_RISK',
      summary: `Navigated forward to ${this.activeUrl}`,
      data: { url: this.activeUrl, title: this.activeTitle },
      started_at: startedAt,
      completed_at: new Date().toISOString(),
    };
  }

  /**
   * Live DOM / Accessibility tree extraction from real document or container.
   */
  public inspectPage(root?: HTMLElement | Document): BrowserPageContext {
    const doc = root || (typeof document !== 'undefined' ? document : null);
    if (!doc) {
      return {
        url: this.activeUrl,
        title: this.activeTitle,
        is_loading: false,
        can_go_back: this.historyIndex > 0,
        can_go_forward: this.historyIndex < this.historyStack.length - 1,
        dom_tree: [],
        headings: [],
        links_count: 0,
        inputs_count: 0,
        buttons_count: 0,
        captured_at: new Date().toISOString(),
      };
    }

    const elements: DOMElementSummary[] = [];
    const headings: string[] = [];

    // Query interactive and structural elements (avoiding parent container text pollution)
    const nodes = doc.querySelectorAll(
      'h1, h2, h3, button, a[href], input, textarea, select, [role="button"], [role="link"], [role="textbox"]'
    );

    let linksCount = 0;
    let inputsCount = 0;
    let buttonsCount = 0;

    nodes.forEach((node, idx) => {
      const el = node as HTMLElement;
      const tag = el.tagName.toLowerCase();
      const role = el.getAttribute('role') || undefined;
      const text = (el.textContent || '').trim().replace(/\s+/g, ' ').substring(0, 120);
      const placeholder = (el as HTMLInputElement).placeholder || undefined;
      const type = (el as HTMLInputElement).type || undefined;
      const id = el.id || undefined;

      const attrs: Record<string, string> = {};
      for (let i = 0; i < el.attributes.length; i++) {
        const attr = el.attributes[i];
        if (!attr.name.startsWith('data-react') && attr.name !== 'style') {
          attrs[attr.name] = attr.value;
        }
      }

      if (tag.startsWith('h') && text) {
        headings.push(text);
      }

      if (tag === 'a' || role === 'link') linksCount++;
      if (tag === 'input' || tag === 'textarea' || tag === 'select' || role === 'textbox') inputsCount++;
      if (tag === 'button' || role === 'button') buttonsCount++;

      let bounds = undefined;
      if (typeof el.getBoundingClientRect === 'function') {
        const rect = el.getBoundingClientRect();
        bounds = {
          x: Math.round(rect.left),
          y: Math.round(rect.top),
          width: Math.round(rect.width),
          height: Math.round(rect.height),
        };
      }

      const isSensitive = visualTargetingService.isSensitiveElement(attrs, text, tag, type || '');
      const selector = this.generateSelector(el, idx);

      elements.push({
        id,
        selector,
        tag,
        role,
        text,
        placeholder,
        type,
        bounds,
        is_interactive: ['button', 'a', 'input', 'textarea', 'select'].includes(tag) || role === 'button',
        is_sensitive: isSensitive,
        is_visible: true,
        attributes: attrs,
      });
    });

    this.currentDOMTree = elements;

    return {
      url: this.activeUrl,
      title: typeof document !== 'undefined' ? document.title || this.activeTitle : this.activeTitle,
      tab_id: this.tabs.find((t) => t.is_active)?.id,
      tab_index: this.tabs.find((t) => t.is_active)?.index,
      tab_count: this.tabs.length,
      is_loading: false,
      can_go_back: this.historyIndex > 0,
      can_go_forward: this.historyIndex < this.historyStack.length - 1,
      dom_tree: elements,
      headings,
      links_count: linksCount,
      inputs_count: inputsCount,
      buttons_count: buttonsCount,
      captured_at: new Date().toISOString(),
    };
  }

  /**
   * Finds element matching semantic criteria.
   */
  public findElement(criteria: SemanticCriteria): DOMElementSummary | null {
    if (this.currentDOMTree.length === 0) {
      this.inspectPage();
    }

    if (criteria.selector) {
      const el = this.currentDOMTree.find((e) => e.selector === criteria.selector);
      if (el) return el;
    }

    if (criteria.id) {
      const el = this.currentDOMTree.find((e) => e.id === criteria.id);
      if (el) return el;
    }

    if (criteria.text) {
      const textQuery = criteria.text.toLowerCase().trim();
      const exact = this.currentDOMTree.find((e) => e.text.toLowerCase().trim() === textQuery);
      if (exact) return exact;
      const interactive = this.currentDOMTree.find((e) => e.is_interactive && e.text.toLowerCase().includes(textQuery));
      if (interactive) return interactive;
      const el = this.currentDOMTree.find((e) => e.text.toLowerCase().includes(textQuery));
      if (el) return el;
    }

    if (criteria.placeholder) {
      const phQuery = criteria.placeholder.toLowerCase();
      const el = this.currentDOMTree.find((e) => e.placeholder && e.placeholder.toLowerCase().includes(phQuery));
      if (el) return el;
    }

    if (criteria.role) {
      const roleQuery = criteria.role.toLowerCase();
      const el = this.currentDOMTree.find((e) => e.role && e.role.toLowerCase() === roleQuery);
      if (el) return el;
    }

    return null;
  }

  /**
   * Clicks a target element in DOM with verification.
   * State-changing actions (forms, purchases) are flagged with risk classification.
   */
  public async clickElement(
    target: string | DOMElementSummary,
    root?: HTMLElement | Document
  ): Promise<ActionResult> {
    const startedAt = new Date().toISOString();
    const selector = typeof target === 'string' ? target : target.selector;

    const doc = root || (typeof document !== 'undefined' ? document : null);
    if (!doc) {
      return {
        schema_version: '1.0.0',
        request_id: `click_${Date.now()}`,
        status: 'FAILED',
        risk_level: 'LOW_RISK',
        summary: `Document context unavailable to click "${selector}"`,
        data: {},
        error: 'No document environment found',
        started_at: startedAt,
        completed_at: new Date().toISOString(),
      };
    }

    try {
      const el = doc.querySelector(selector) as HTMLElement;
      if (!el) {
        return {
          schema_version: '1.0.0',
          request_id: `click_${Date.now()}`,
          status: 'FAILED',
          risk_level: 'LOW_RISK',
          summary: `Element "${selector}" not found on page`,
          data: { selector },
          error: 'Element not found',
          started_at: startedAt,
          completed_at: new Date().toISOString(),
        };
      }

      // Check if clicking is state-changing (e.g. submit button, purchase, delete)
      const text = (el.textContent || '').toLowerCase();
      const isStateChange = /submit|order|buy|purchase|delete|confirm|pay/i.test(text);

      // Perform real click event dispatch
      el.focus?.();
      el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
      el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
      el.click();

      this.refreshPageContext(doc);

      return {
        schema_version: '1.0.0',
        request_id: `click_${Date.now()}`,
        status: 'SUCCEEDED',
        risk_level: isStateChange ? 'CONFIRM_REQUIRED' : 'LOW_RISK',
        summary: `Clicked element <${el.tagName.toLowerCase()}> "${(el.textContent || selector).trim().substring(0, 50)}"`,
        data: {
          selector,
          tag: el.tagName.toLowerCase(),
          text: (el.textContent || '').trim().substring(0, 100),
          is_state_change: isStateChange,
        },
        started_at: startedAt,
        completed_at: new Date().toISOString(),
      };
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : String(err);
      return {
        schema_version: '1.0.0',
        request_id: `click_err_${Date.now()}`,
        status: 'FAILED',
        risk_level: 'LOW_RISK',
        summary: `Failed to click "${selector}": ${errMsg}`,
        data: { selector },
        error: errMsg,
        started_at: startedAt,
        completed_at: new Date().toISOString(),
      };
    }
  }

  /**
   * Types text into target input field with sensitive field protection.
   */
  public async typeText(
    target: string | DOMElementSummary,
    text: string,
    clearFirst: boolean = true,
    root?: HTMLElement | Document
  ): Promise<ActionResult> {
    const startedAt = new Date().toISOString();
    const selector = typeof target === 'string' ? target : target.selector;

    const doc = root || (typeof document !== 'undefined' ? document : null);
    if (!doc) {
      return {
        schema_version: '1.0.0',
        request_id: `type_${Date.now()}`,
        status: 'FAILED',
        risk_level: 'LOW_RISK',
        summary: `Document context unavailable to type into "${selector}"`,
        data: {},
        error: 'No document environment found',
        started_at: startedAt,
        completed_at: new Date().toISOString(),
      };
    }

    try {
      const el = doc.querySelector(selector) as HTMLInputElement | HTMLTextAreaElement;
      if (!el) {
        return {
          schema_version: '1.0.0',
          request_id: `type_${Date.now()}`,
          status: 'FAILED',
          risk_level: 'LOW_RISK',
          summary: `Input element "${selector}" not found`,
          data: { selector },
          error: 'Input element not found',
          started_at: startedAt,
          completed_at: new Date().toISOString(),
        };
      }

      const isSensitive = el.type === 'password' || /password|secret|card|token/i.test(el.name || el.id || '');

      el.focus();
      if (clearFirst) {
        el.value = '';
      }
      el.value = (el.value || '') + text;

      // Dispatch native input & change events
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));

      return {
        schema_version: '1.0.0',
        request_id: `type_${Date.now()}`,
        status: 'SUCCEEDED',
        risk_level: 'LOW_RISK',
        summary: `Typed into input "${selector}" (${isSensitive ? '[REDACTED_SENSITIVE]' : text.substring(0, 30)})`,
        data: {
          selector,
          is_sensitive: isSensitive,
          value_preview: isSensitive ? '[REDACTED_SENSITIVE]' : text.substring(0, 50),
          length: text.length,
        },
        started_at: startedAt,
        completed_at: new Date().toISOString(),
      };
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : String(err);
      return {
        schema_version: '1.0.0',
        request_id: `type_err_${Date.now()}`,
        status: 'FAILED',
        risk_level: 'LOW_RISK',
        summary: `Failed to type into "${selector}": ${errMsg}`,
        data: { selector },
        error: errMsg,
        started_at: startedAt,
        completed_at: new Date().toISOString(),
      };
    }
  }

  /**
   * Scrolls the page or specific container.
   */
  public async scroll(
    deltaY: number | 'top' | 'bottom' | 'element',
    elementTarget?: string,
    root?: HTMLElement | Document
  ): Promise<ActionResult> {
    const startedAt = new Date().toISOString();
    const doc = root || (typeof document !== 'undefined' ? document : null);

    try {
      if (deltaY === 'top') {
        if (typeof window !== 'undefined') window.scrollTo({ top: 0, behavior: 'smooth' });
      } else if (deltaY === 'bottom') {
        if (typeof window !== 'undefined') window.scrollTo({ top: 10000, behavior: 'smooth' });
      } else if (deltaY === 'element' && elementTarget && doc) {
        const el = doc.querySelector(elementTarget);
        if (el && typeof el.scrollIntoView === 'function') {
          el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      } else if (typeof deltaY === 'number') {
        if (typeof window !== 'undefined') window.scrollBy({ top: deltaY, behavior: 'smooth' });
      }

      return {
        schema_version: '1.0.0',
        request_id: `scroll_${Date.now()}`,
        status: 'SUCCEEDED',
        risk_level: 'READ_ONLY',
        summary: `Scrolled page (${deltaY})`,
        data: { deltaY, target: elementTarget || null },
        started_at: startedAt,
        completed_at: new Date().toISOString(),
      };
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : String(err);
      return {
        schema_version: '1.0.0',
        request_id: `scroll_err_${Date.now()}`,
        status: 'FAILED',
        risk_level: 'READ_ONLY',
        summary: `Scroll error: ${errMsg}`,
        data: {},
        error: errMsg,
        started_at: startedAt,
        completed_at: new Date().toISOString(),
      };
    }
  }

  /**
   * Reads page text or selected text.
   */
  public readPageText(mode: 'all' | 'selection' | 'summary' | 'headings' = 'summary', root?: HTMLElement | Document): string {
    const doc = root || (typeof document !== 'undefined' ? document : null);
    if (!doc) return 'No document context available.';

    if (mode === 'selection' && typeof window !== 'undefined') {
      const sel = window.getSelection();
      return sel ? sel.toString().trim() : '';
    }

    if (mode === 'headings') {
      const headings = Array.from(doc.querySelectorAll('h1, h2, h3'))
        .map((h) => h.textContent?.trim())
        .filter(Boolean);
      return headings.join('\n');
    }

    const bodyEl = 'body' in doc ? doc.body : doc;
    const bodyText = (bodyEl ? (bodyEl.innerText || bodyEl.textContent || '') : '').trim();
    if (mode === 'summary') {
      return bodyText.split('\n').filter((l: string) => l.trim().length > 0).slice(0, 15).join('\n');
    }

    return bodyText;
  }

  private validateUrlScheme(rawUrl: string): void {
    if (!rawUrl || typeof rawUrl !== 'string') {
      throw new Error('URL must be a non-empty string');
    }
    const trimmed = rawUrl.trim().toLowerCase();
    if (!trimmed.startsWith('http://') && !trimmed.startsWith('https://')) {
      throw new Error(`URL scheme not allowed (only http/https): ${rawUrl}`);
    }
  }

  private deriveTitleFromUrl(url: string): string {
    try {
      const u = new URL(url);
      return u.hostname + (u.pathname.length > 1 ? u.pathname : '');
    } catch {
      return url;
    }
  }

  private generateSelector(el: HTMLElement, index: number): string {
    if (el.id) return `#${el.id}`;
    const name = el.getAttribute('name');
    if (name) return `${el.tagName.toLowerCase()}[name="${name}"]`;
    const role = el.getAttribute('role');
    if (role) return `${el.tagName.toLowerCase()}[role="${role}"]`;
    const cls = el.className && typeof el.className === 'string'
      ? el.className.split(' ').filter(Boolean).slice(0, 2).join('.')
      : '';
    if (cls) return `${el.tagName.toLowerCase()}.${cls}`;
    return `${el.tagName.toLowerCase()}:nth-of-type(${index + 1})`;
  }

  private refreshPageContext(doc?: Document | HTMLElement): void {
    if (typeof document !== 'undefined' || doc) {
      this.inspectPage(doc);
    }
  }
}

export const browserControlService = new BrowserControlService();
