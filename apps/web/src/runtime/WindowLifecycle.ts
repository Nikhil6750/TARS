/**
 * Window show/hide/quit semantics for the voice-first panel, kept in one
 * place instead of scattered across components:
 *
 *   X on the panel      -> hide window, background TARS keeps running
 *   Escape               -> hide panel
 *   "Hey TARS" / summon   -> restore/show panel (native emits tars://summon-hud)
 *   Tray "Quit TARS"     -> full process exit (native-side, see lib.rs exit_app)
 *
 * Hiding never stops the native wake engine -- see wake_engine.rs; this
 * module only ever calls window show/hide, never anything mic-related.
 */
import { nativeBridge } from '../services/native-bridge';
import { isTauri } from '../services/tauri';

export type SummonMode = 'voice' | 'compact' | 'hud' | 'full' | 'workstation' | 'pill';

type UnlistenFn = () => void;

export class WindowLifecycle {
  private unlisten: UnlistenFn[] = [];

  public async start(onSummon: (mode: SummonMode) => void): Promise<void> {
    document.addEventListener('keydown', this.handleKeyDown);

    if (!isTauri()) return;
    try {
      const { listen } = await import('@tauri-apps/api/event');
      const un = await listen<string>('tars://summon-hud', (e) => {
        const mode = (e.payload || 'voice') as SummonMode;
        onSummon(mode);
      });
      this.unlisten.push(un);
    } catch (err) {
      console.warn('[WindowLifecycle] Failed to listen for summon events:', err);
    }
  }

  public stop(): void {
    document.removeEventListener('keydown', this.handleKeyDown);
    this.unlisten.forEach((fn) => fn());
    this.unlisten = [];
  }

  private handleKeyDown = (event: KeyboardEvent): void => {
    if (event.key === 'Escape') {
      void this.hide();
    }
  };

  public async hide(): Promise<void> {
    await nativeBridge.hideHUD();
  }

  public async summon(mode: SummonMode = 'voice'): Promise<void> {
    await nativeBridge.summonHUD(mode);
  }

  /** Tray "Quit TARS" is native-only (tray menu -> exit_app); exposed here for an in-app quit action if ever needed. */
  public async quit(): Promise<void> {
    await nativeBridge.exitApp();
  }
}

export const windowLifecycle = new WindowLifecycle();
