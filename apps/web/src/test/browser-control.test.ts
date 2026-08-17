import { describe, it, expect, beforeEach } from 'vitest';
import { BrowserControlService } from '../services/browser-control';

describe('Wave 2B Browser Automation & Context Service', () => {
  let browserService: BrowserControlService;

  beforeEach(() => {
    browserService = new BrowserControlService();
    // Setup simulated HTML document in JSDOM
    document.body.innerHTML = `
      <div id="test-app">
        <h1>Trading Dashboard</h1>
        <h2>Market Overview</h2>
        <nav>
          <a href="/markets" id="link-markets">Markets</a>
          <a href="/orders" id="link-orders">Orders</a>
        </nav>
        <form id="order-form">
          <input type="text" id="symbol-input" name="symbol" placeholder="Enter ticker symbol" />
          <input type="password" id="api-key-input" name="api_key" placeholder="Enter API secret" />
          <button type="button" id="search-btn">Search Ticker</button>
          <button type="submit" id="buy-order-btn">Submit Buy Order</button>
        </form>
        <div id="content-section">
          <p>Real-time statistical market intelligence powered by deterministic quant models.</p>
        </div>
      </div>
    `;
  });

  describe('URL Navigation & Scheme Validation', () => {
    it('navigates to safe HTTP and HTTPS URLs', async () => {
      const res = await browserService.navigate('https://tradingview.com/chart');
      expect(res.status).toBe('SUCCEEDED');
      expect(res.data.url).toBe('https://tradingview.com/chart');

      const ctx = browserService.getCurrentContext();
      expect(ctx.url).toBe('https://tradingview.com/chart');
    });

    it('rejects unsafe schemes like javascript: and file://', async () => {
      const res1 = await browserService.navigate('javascript:alert(1)');
      expect(res1.status).toBe('FAILED');
      expect(res1.error).toContain('scheme not allowed');

      const res2 = await browserService.navigate('file:///C:/passwords.txt');
      expect(res2.status).toBe('FAILED');
    });
  });

  describe('DOM & Accessibility Structure Inspection', () => {
    it('inspects page and extracts interactive elements and headings hierarchy', () => {
      const pageCtx = browserService.inspectPage(document);
      expect(pageCtx.headings).toContain('Trading Dashboard');
      expect(pageCtx.headings).toContain('Market Overview');
      expect(pageCtx.links_count).toBeGreaterThanOrEqual(2);
      expect(pageCtx.inputs_count).toBeGreaterThanOrEqual(2);
      expect(pageCtx.buttons_count).toBeGreaterThanOrEqual(2);
      expect(pageCtx.dom_tree?.length).toBeGreaterThan(0);
    });

    it('locates elements semantically by text, placeholder, or selector', () => {
      const el1 = browserService.findElement({ text: 'Search Ticker' });
      expect(el1).toBeDefined();
      expect(el1?.tag).toBe('button');

      const el2 = browserService.findElement({ placeholder: 'Enter ticker symbol' });
      expect(el2).toBeDefined();
      expect(el2?.id).toBe('symbol-input');

      const el3 = browserService.findElement({ selector: '#buy-order-btn' });
      expect(el3).toBeDefined();
      expect(el3?.text).toContain('Submit Buy Order');
    });
  });

  describe('Safe Browser Actions: Click, Type, Scroll', () => {
    it('clicks a safe button and returns execution result', async () => {
      const res = await browserService.clickElement('#search-btn', document);
      expect(res.status).toBe('SUCCEEDED');
      expect(res.risk_level).toBe('LOW_RISK');
    });

    it('flags state-changing order submission buttons with CONFIRM_REQUIRED', async () => {
      const res = await browserService.clickElement('#buy-order-btn', document);
      expect(res.status).toBe('SUCCEEDED');
      expect(res.risk_level).toBe('CONFIRM_REQUIRED');
      expect(res.data.is_state_change).toBe(true);
    });

    it('types into safe input fields and updates element value', async () => {
      const res = await browserService.typeText('#symbol-input', 'BTC/USDT', true, document);
      expect(res.status).toBe('SUCCEEDED');

      const input = document.getElementById('symbol-input') as HTMLInputElement;
      expect(input.value).toBe('BTC/USDT');
    });

    it('protects sensitive password fields by redacting values in returned data payload', async () => {
      const res = await browserService.typeText('#api-key-input', 'super_secret_token_123', true, document);
      expect(res.status).toBe('SUCCEEDED');
      expect(res.data.is_sensitive).toBe(true);
      expect(res.data.value_preview).toBe('[REDACTED_SENSITIVE]');

      // Native input value in DOM is still set correctly
      const input = document.getElementById('api-key-input') as HTMLInputElement;
      expect(input.value).toBe('super_secret_token_123');
    });

    it('scrolls the page smoothly without exceptions', async () => {
      const res = await browserService.scroll(300, undefined, document);
      expect(res.status).toBe('SUCCEEDED');
      expect(res.risk_level).toBe('READ_ONLY');
    });
  });

  describe('Browser History Navigation', () => {
    it('handles back and forward navigation stack', async () => {
      await browserService.navigate('https://example.com/page1');
      await browserService.navigate('https://example.com/page2');

      expect(browserService.getCurrentContext().canGoBack).toBe(true);

      const backRes = await browserService.back();
      expect(backRes.status).toBe('SUCCEEDED');
      expect(browserService.getCurrentContext().url).toBe('https://example.com/page1');

      const fwdRes = await browserService.forward();
      expect(fwdRes.status).toBe('SUCCEEDED');
      expect(browserService.getCurrentContext().url).toBe('https://example.com/page2');
    });
  });

  describe('Tab Management', () => {
    it('enumerates, opens, switches, and closes browser tabs', () => {
      const initialTabs = browserService.getTabs();
      expect(initialTabs.length).toBe(1);

      const newTab = browserService.openTab('https://news.ycombinator.com', 'Hacker News');
      expect(browserService.getTabs().length).toBe(2);
      expect(newTab.is_active).toBe(true);

      const switched = browserService.switchTab(0);
      expect(switched).toBeDefined();
      expect(switched?.index).toBe(0);

      const closed = browserService.closeTab(newTab.id);
      expect(closed).toBe(true);
      expect(browserService.getTabs().length).toBe(1);
    });
  });

  describe('Page Text Extraction', () => {
    it('extracts page text summary and headings', () => {
      const summary = browserService.readPageText('summary', document);
      expect(summary).toContain('Trading Dashboard');

      const headings = browserService.readPageText('headings', document);
      expect(headings).toContain('Trading Dashboard');
      expect(headings).toContain('Market Overview');
    });
  });
});
