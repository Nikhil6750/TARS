import React, { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { OrbStore, orbStore as defaultStore } from '../../orb/orbStore';
import { orbNative } from '../../orb/orbNative';
import { TarsOrb } from './TarsOrb';
import { VoiceBar } from './VoiceBar';

/** Window layout constants (logical px) — must match ORB_W/ORB_H in lib.rs. */
export const ORB_WINDOW = { w: 260, h: 230 };
const CANVAS = 168;
const HIT = 120; // interactive square around the orb body
const HIT_X = (ORB_WINDOW.w - HIT) / 2;
const HIT_Y = CANVAS / 2 - HIT / 2;

export interface OrbActions {
  /** Ask the backend session to start listening now (opens Gemini Live). */
  wake: () => void;
  setMuted: (muted: boolean) => void;
  confirm: (approve: boolean) => void;
  openWorkspace: (section?: 'settings') => void;
  quit: () => void;
}

interface Props {
  store?: OrbStore;
  actions: OrbActions;
}

const DRAG_THRESHOLD = 5;
const DOUBLE_CLICK_MS = 260;

export const OrbCompanion: React.FC<Props> = ({ store = defaultStore, actions }) => {
  const snap = useSyncExternalStore(store.subscribe, store.getSnapshot);
  const [menu, setMenu] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const bubbleRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const press = useRef<{ x: number; y: number; dragging: boolean } | null>(null);
  const clickTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const activate = useCallback(() => {
    store.attend();
    actions.wake();
  }, [store, actions]);

  const onPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return;
    press.current = { x: e.clientX, y: e.clientY, dragging: false };
    (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
    setMenu(false);
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const p = press.current;
    if (!p || p.dragging) return;
    if (Math.hypot(e.clientX - p.x, e.clientY - p.y) > DRAG_THRESHOLD) {
      p.dragging = true; // a drag never counts as a click: no accidental voice activation
      void orbNative.startDrag();
    }
  };
  const onPointerUp = (e: React.PointerEvent) => {
    const p = press.current;
    press.current = null;
    (e.currentTarget as HTMLElement).releasePointerCapture?.(e.pointerId);
    if (!p) return;
    if (p.dragging) {
      void orbNative.savePosition();
      return;
    }
    if (clickTimer.current) {
      // second click within the window = expand to the full workspace
      clearTimeout(clickTimer.current);
      clickTimer.current = null;
      actions.openWorkspace();
      return;
    }
    clickTimer.current = setTimeout(() => {
      clickTimer.current = null;
      activate();
    }, DOUBLE_CLICK_MS);
  };
  const onContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    setMenu(true);
  };
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && e.shiftKey) {
      e.preventDefault();
      actions.openWorkspace();
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      activate();
    } else if (e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10')) {
      e.preventDefault();
      setMenu(true);
    } else if (e.key === 'Escape') {
      setMenu(false);
    }
  };

  useEffect(() => () => {
    if (clickTimer.current) clearTimeout(clickTimer.current);
  }, []);

  // Report which rectangles are interactive so all other transparent pixels are click-through.
  useEffect(() => {
    const regions: Array<[number, number, number, number]> = [[HIT_X, HIT_Y, HIT, HIT]];
    for (const el of [bubbleRef.current, menuRef.current]) {
      if (el) regions.push([el.offsetLeft, el.offsetTop, el.offsetWidth, el.offsetHeight]);
    }
    void orbNative.setHitRegions(regions);
  }, [snap.bubble, snap.confirm, snap.transcript, snap.caption, menu]);

  useEffect(() => {
    if (!menu) return;
    const close = (e: MouseEvent) => {
      if (!menuRef.current?.contains(e.target as Node)) setMenu(false);
    };
    window.addEventListener('mousedown', close);
    return () => window.removeEventListener('mousedown', close);
  }, [menu]);

  const item = (label: string, run: () => void) => (
    <button
      key={label}
      role="menuitem"
      type="button"
      onClick={() => { setMenu(false); run(); }}
      className="w-full text-left text-[12px] text-slate-100 hover:bg-white/10 focus:bg-white/10 outline-none rounded-md"
      style={{ padding: '6px 12px' }}
    >
      {label}
    </button>
  );

  const line = snap.transcript || snap.caption;
  const stateLabel = snap.state.toLowerCase().replace(/_/g, ' ');

  return (
    <div
      ref={rootRef}
      data-testid="orb-companion"
      data-orb-state={snap.state}
      className="relative select-none"
      style={{ width: ORB_WINDOW.w, height: ORB_WINDOW.h, background: 'transparent' }}
    >
      {/* The orb: the only permanent element. */}
      <div style={{ position: 'absolute', left: (ORB_WINDOW.w - CANVAS) / 2, top: 0, opacity: snap.muted ? 0.5 : 1, transition: 'opacity 250ms' }}>
        <TarsOrb state={snap.state} flashUntil={snap.flashUntil} size={CANVAS} />
      </div>
      <div
        role="button"
        tabIndex={0}
        aria-label={`TARS, ${stateLabel}${snap.muted ? ', muted' : ''}. Click to talk, double-click to open the workspace, right-click for options.`}
        data-testid="orb-hit"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onContextMenu={onContextMenu}
        onKeyDown={onKeyDown}
        className="rounded-full outline-none focus-visible:ring-2 focus-visible:ring-sky-300/60"
        style={{ position: 'absolute', left: HIT_X, top: HIT_Y, width: HIT, height: HIT, cursor: 'pointer', touchAction: 'none' }}
      />

      {/* Waveform: only while voice is active. */}
      <div style={{ position: 'absolute', left: (ORB_WINDOW.w - 104) / 2, top: CANVAS - 20, height: 22 }}>
        <VoiceBar state={snap.state} />
      </div>

      {/* Short-lived text: never a permanent card. */}
      {snap.confirm ? (
        <div
          ref={bubbleRef}
          role="alertdialog"
          aria-label="Confirm action"
          className="absolute rounded-xl bg-slate-900/85 backdrop-blur px-3 py-2 text-slate-100 shadow-lg"
          style={{ left: 16, top: CANVAS + 6, width: ORB_WINDOW.w - 32, padding: '8px 12px' }}
        >
          <div className="text-[11px] leading-snug mb-1.5 line-clamp-2">{snap.confirm.text}</div>
          <div className="flex gap-2">
            <button type="button" onClick={() => actions.confirm(true)} className="flex-1 rounded-md bg-sky-500/80 hover:bg-sky-400 text-[11px] text-white" style={{ padding: '4px 0' }}>Yes</button>
            <button type="button" onClick={() => actions.confirm(false)} className="flex-1 rounded-md bg-white/10 hover:bg-white/20 text-[11px]" style={{ padding: '4px 0' }}>No</button>
          </div>
        </div>
      ) : snap.bubble ? (
        <button
          ref={bubbleRef as unknown as React.RefObject<HTMLButtonElement>}
          type="button"
          data-testid="orb-bubble"
          onClick={() => { store.dismissBubble(); actions.openWorkspace(); }}
          className={`absolute rounded-xl px-3 py-1.5 text-left text-[11.5px] leading-snug backdrop-blur shadow-lg ${
            snap.bubble.kind === 'alert' ? 'bg-amber-950/80 text-amber-50' : snap.bubble.kind === 'error' ? 'bg-rose-950/80 text-rose-50' : 'bg-slate-900/85 text-slate-100'
          }`}
          style={{ left: 16, top: CANVAS + 6, width: ORB_WINDOW.w - 32, padding: '6px 12px' }}
          aria-label={`${snap.bubble.text}. Open workspace.`}
        >
          <span className="line-clamp-2">{snap.bubble.replay && !/replay/i.test(snap.bubble.text) ? '[Replay] ' : ''}{snap.bubble.text}</span>
        </button>
      ) : line ? (
        <div
          data-testid="orb-transcript"
          className="absolute text-center text-[12px] leading-snug text-white/90 pointer-events-none"
          style={{ left: 14, top: CANVAS + 8, width: ORB_WINDOW.w - 28, textShadow: '0 1px 6px rgba(0,0,0,0.65)' }}
        >
          <span className="line-clamp-2">{snap.transcript ? `“${snap.transcript}”` : snap.caption}</span>
        </div>
      ) : null}

      {menu && (
        <div
          ref={menuRef}
          role="menu"
          aria-label="TARS options"
          className="absolute rounded-xl bg-slate-900/92 backdrop-blur shadow-xl"
          style={{ left: (ORB_WINDOW.w - 176) / 2, top: 30, width: 176, padding: 4 }}
        >
          {item('Open TARS', () => actions.openWorkspace())}
          {item('Start listening', activate)}
          {item(snap.muted ? 'Unmute TARS' : 'Mute TARS', () => { actions.setMuted(!snap.muted); store.setMuted(!snap.muted); })}
          {snap.lastAlert && item('Recent alert', () => store.showLastAlert())}
          {item('Settings', () => actions.openWorkspace('settings'))}
          {item('Quit TARS', actions.quit)}
        </div>
      )}
    </div>
  );
};
