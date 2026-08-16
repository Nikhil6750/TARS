import { describe, it, expect } from 'vitest';
import { TARSTradingEvent } from '../types/trading-event';

/**
 * Pure lifecycle reducer logic matching App.tsx active setups state transitions
 */
function updateActiveSetups(
  prevSetups: TARSTradingEvent[],
  event: TARSTradingEvent
): TARSTradingEvent[] {
  const shouldClear =
    event.state === 'IDLE' ||
    event.state === 'SETUP_INVALIDATED' ||
    event.validation_status === 'INVALID' ||
    event.validation_status === 'EXPIRED';

  if (shouldClear) {
    return prevSetups.filter((s) => s.symbol !== event.symbol);
  }

  if (event.state === 'SETUP_DEVELOPING' || event.state === 'SETUP_VALID') {
    const existingIdx = prevSetups.findIndex((s) => s.symbol === event.symbol);
    if (existingIdx >= 0) {
      const next = [...prevSetups];
      next[existingIdx] = event;
      return next;
    }
    return [event, ...prevSetups];
  }

  return prevSetups;
}

describe('Trading Event Lifecycle State Reducer', () => {
  it('adds new developing and valid setups to active collection', () => {
    let setups: TARSTradingEvent[] = [];

    const devEvent: TARSTradingEvent = {
      schema_version: '1.0.0',
      event_id: 'e1',
      timestamp: new Date().toISOString(),
      source: 'mock',
      symbol: 'NQ',
      state: 'SETUP_DEVELOPING',
      validation_status: 'PENDING',
      direction: 'SHORT',
      entry: 20400,
      stop_loss: 20450,
      take_profit: 20300,
      risk_reward: 2.0
    };

    setups = updateActiveSetups(setups, devEvent);
    expect(setups.length).toBe(1);
    expect(setups[0].symbol).toBe('NQ');
    expect(setups[0].state).toBe('SETUP_DEVELOPING');

    const validEvent: TARSTradingEvent = {
      schema_version: '1.0.0',
      event_id: 'e2',
      timestamp: new Date().toISOString(),
      source: 'mock',
      symbol: 'XAUUSD',
      state: 'SETUP_VALID',
      validation_status: 'VALID',
      direction: 'LONG',
      entry: 2684.5,
      stop_loss: 2676.0,
      take_profit: 2708.5,
      risk_reward: 2.82
    };

    setups = updateActiveSetups(setups, validEvent);
    expect(setups.length).toBe(2);
  });

  it('updates an existing symbol when state transitions from SETUP_DEVELOPING to SETUP_VALID', () => {
    const devEvent: TARSTradingEvent = {
      schema_version: '1.0.0',
      event_id: 'e1',
      timestamp: new Date().toISOString(),
      source: 'mock',
      symbol: 'ES',
      state: 'SETUP_DEVELOPING',
      validation_status: 'PENDING',
      direction: 'LONG',
      entry: 5880.0,
      stop_loss: 5865.0,
      take_profit: 5925.0,
      risk_reward: 3.0
    };

    let setups = updateActiveSetups([], devEvent);
    expect(setups[0].validation_status).toBe('PENDING');

    const validEvent: TARSTradingEvent = {
      ...devEvent,
      event_id: 'e1_valid',
      state: 'SETUP_VALID',
      validation_status: 'VALID'
    };

    setups = updateActiveSetups(setups, validEvent);
    expect(setups.length).toBe(1);
    expect(setups[0].state).toBe('SETUP_VALID');
    expect(setups[0].validation_status).toBe('VALID');
  });

  it('removes setup from active collection on SETUP_INVALIDATED', () => {
    const validEvent: TARSTradingEvent = {
      schema_version: '1.0.0',
      event_id: 'e1',
      timestamp: new Date().toISOString(),
      source: 'mock',
      symbol: 'BTCUSD',
      state: 'SETUP_VALID',
      validation_status: 'VALID',
      direction: 'SHORT',
      entry: 96000,
      stop_loss: 97000,
      take_profit: 94000,
      risk_reward: 2.0
    };

    let setups = [validEvent];
    expect(setups.length).toBe(1);

    const invalidatedEvent: TARSTradingEvent = {
      ...validEvent,
      event_id: 'e1_inv',
      state: 'SETUP_INVALIDATED',
      validation_status: 'INVALID'
    };

    setups = updateActiveSetups(setups, invalidatedEvent);
    expect(setups.length).toBe(0);
  });

  it('removes setup from active collection on EXPIRED or IDLE', () => {
    const validEvent: TARSTradingEvent = {
      schema_version: '1.0.0',
      event_id: 'e1',
      timestamp: new Date().toISOString(),
      source: 'mock',
      symbol: 'USO',
      state: 'SETUP_VALID',
      validation_status: 'VALID',
      direction: 'LONG',
      entry: 78.0,
      stop_loss: 76.5,
      take_profit: 82.0,
      risk_reward: 2.67
    };

    let setups = [validEvent];

    const idleEvent: TARSTradingEvent = {
      ...validEvent,
      event_id: 'e1_idle',
      state: 'IDLE',
      validation_status: 'EXPIRED'
    };

    setups = updateActiveSetups(setups, idleEvent);
    expect(setups.length).toBe(0);
  });
});
