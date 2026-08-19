import { describe, it, expect } from 'vitest';
import { tryDeterministicAnswer } from '../services/deterministic-fast-path';

describe('tryDeterministicAnswer', () => {
  it('evaluates plain arithmetic', () => {
    expect(tryDeterministicAnswer('7 * 6')).toBe('42');
    expect(tryDeterministicAnswer('12 + 5')).toBe('17');
    expect(tryDeterministicAnswer('100 / 4')).toBe('25');
    expect(tryDeterministicAnswer('9 - 15')).toBe('-6');
  });

  it('handles question-style phrasing and punctuation', () => {
    expect(tryDeterministicAnswer('What is 7 * 6?')).toBe('42');
    expect(tryDeterministicAnswer("what's 7*6")).toBe('42');
    expect(tryDeterministicAnswer('Calculate 100 / 4')).toBe('25');
  });

  it('handles x/× as multiplication and ÷ as division', () => {
    expect(tryDeterministicAnswer('7 x 6')).toBe('42');
    expect(tryDeterministicAnswer('7 × 6')).toBe('42');
    expect(tryDeterministicAnswer('20 ÷ 4')).toBe('5');
  });

  it('respects operator precedence and parentheses', () => {
    expect(tryDeterministicAnswer('2 + 3 * 4')).toBe('14');
    expect(tryDeterministicAnswer('(2 + 3) * 4')).toBe('20');
  });

  it('returns decimal results rounded sanely, not integers when not whole', () => {
    expect(tryDeterministicAnswer('7 / 2')).toBe('3.5');
  });

  it('falls through (returns null) for non-arithmetic text', () => {
    expect(tryDeterministicAnswer('What is the capital of Japan?')).toBeNull();
    expect(tryDeterministicAnswer('Analyze this chart')).toBeNull();
    expect(tryDeterministicAnswer('Active setups')).toBeNull();
    expect(tryDeterministicAnswer('')).toBeNull();
  });

  it('falls through for malformed or unsafe-looking expressions rather than guessing', () => {
    expect(tryDeterministicAnswer('7 * ')).toBeNull();
    expect(tryDeterministicAnswer('7 / 0')).toBeNull();
    expect(tryDeterministicAnswer('alert(1) + 1')).toBeNull();
    expect(tryDeterministicAnswer('7 * 6; process.exit()')).toBeNull();
  });
});
