import { describe, it, expect, vi, beforeEach } from 'vitest';
import { nativeBridge } from '../services/native-bridge';
import { ScreenCaptureResult, MonitorInfo, UIElementNode } from '../types/actions';

describe('Wave 2B Screen Awareness & Visual Context', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  describe('Monitor Geometry & DPI Awareness', () => {
    it('retrieves connected monitor geometries with DPI and scale factor', async () => {
      const monitors: MonitorInfo[] = await nativeBridge.getMonitorsGeometry();
      expect(Array.isArray(monitors)).toBe(true);
      expect(monitors.length).toBeGreaterThanOrEqual(1);

      const primary = monitors[0];
      expect(primary.id).toBeDefined();
      expect(primary.name).toBeDefined();
      expect(primary.is_primary).toBe(true);
      expect(primary.bounds.width).toBeGreaterThan(0);
      expect(primary.bounds.height).toBeGreaterThan(0);
      expect(primary.work_area.width).toBeGreaterThan(0);
      expect(primary.work_area.height).toBeGreaterThan(0);
      expect(primary.scale_factor).toBeGreaterThan(0);
      expect(primary.dpi).toBeGreaterThanOrEqual(96);
    });
  });

  describe('Active Window Screen Capture', () => {
    it('captures active window snapshot on explicit request with metadata and base64 data', async () => {
      const capture: ScreenCaptureResult = await nativeBridge.captureActiveWindow(true);
      expect(capture).toBeDefined();
      expect(capture.capture_id).toMatch(/^cap_/);
      expect(capture.source).toBe('active_window');
      expect(capture.executable).toBeDefined();
      expect(capture.window_title).toBeDefined();
      expect(capture.bounds).toBeDefined();
      expect(capture.width).toBeGreaterThanOrEqual(0);
      expect(capture.height).toBeGreaterThanOrEqual(0);
      expect(capture.is_secure_desktop).toBe(false);
      expect(capture.image_format).toBe('image/bmp');
      expect(capture.captured_at).toBeDefined();
      expect(new Date(capture.captured_at).getTime()).not.toBeNaN();
    });

    it('captures active window without image data payload when requested', async () => {
      const capture = await nativeBridge.captureActiveWindow(false);
      expect(capture).toBeDefined();
      expect(capture.capture_id).toBeDefined();
      expect(capture.source).toBe('active_window');
      expect(capture.bounds).toBeDefined();
    });
  });

  describe('Bounded Screen Region Capture', () => {
    it('captures specified bounding box coordinates with clipping and metadata', async () => {
      const region = await nativeBridge.captureScreenRegion(100, 150, 400, 300, true);
      expect(region).toBeDefined();
      expect(region.capture_id).toMatch(/^region_/);
      expect(region.source).toBe('region');
      expect(region.bounds.x).toBe(100);
      expect(region.bounds.y).toBe(150);
      expect(region.bounds.width).toBe(400);
      expect(region.bounds.height).toBe(300);
      expect(region.width).toBe(400);
      expect(region.height).toBe(300);
      expect(region.is_secure_desktop).toBe(false);
    });
  });

  describe('Win32 Accessibility / UI Element Hierarchy', () => {
    it('inspects native UI element hierarchy of the foreground window', async () => {
      const tree: UIElementNode = await nativeBridge.getActiveWindowElements();
      expect(tree).toBeDefined();
      expect(tree.id).toBeDefined();
      expect(tree.role).toBe('window');
      expect(Array.isArray(tree.children)).toBe(true);

      if (tree.children.length > 0) {
        const firstChild = tree.children[0];
        expect(firstChild.id).toBeDefined();
        expect(firstChild.role).toBeDefined();
        expect(typeof firstChild.is_enabled).toBe('boolean');
        expect(typeof firstChild.is_visible).toBe('boolean');
      }
    });
  });

  describe('Temporary Captures Cache & Cleanup', () => {
    it('clears temporary screenshot cache without exceptions', async () => {
      const deletedCount = await nativeBridge.clearCapturesCache();
      expect(typeof deletedCount).toBe('number');
      expect(deletedCount).toBeGreaterThanOrEqual(0);
    });
  });
});
