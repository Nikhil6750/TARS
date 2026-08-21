import { describe, it, expect } from 'vitest';
import { composeSpeech } from '../services/speech';

describe('Speech Sanitization & composeSpeech', () => {
  it('removes markdown markers, URLs, Windows paths, and code blocks', () => {
    const raw = `### XAUUSD · 15M

**Trade Status**
- **NO VALIDATED TRADE**
- Details: https://example.test/report
- File: C:\\TARS\\scratch\\chart.png

\`\`\`python
print('do not read this')
\`\`\`
`;
    const speech = composeSpeech(raw);
    expect(speech).toContain('NO VALIDATED TRADE');
    expect(speech).toContain('Code example omitted');
    for (const marker of ['**', '###', '```', 'http', 'C:\\TARS', '- ']) {
      expect(speech).not.toContain(marker);
    }
  });

  it('preserves clean conversational text and strips headers and bullet asterisks', () => {
    const raw = `## Market Overview
* Price is testing the **1.0850** resistance level.
* Invalidation: below 1.0800.
`;
    const speech = composeSpeech(raw);
    expect(speech).toBe('Market Overview Price is testing the 1.0850 resistance level. Invalidation: below 1.0800.');
  });

  it('handles links by keeping link text only', () => {
    const raw = 'Check out [the economic calendar](https://example.com/calendar) for upcoming FOMC release.';
    const speech = composeSpeech(raw);
    expect(speech).toBe('Check out the economic calendar for upcoming FOMC release.');
  });

  it('bounds long speech with clean word-boundary truncation', () => {
    const raw = 'Word '.repeat(200);
    const speech = composeSpeech(raw, 50);
    expect(speech.length).toBeLessThanOrEqual(51);
    expect(speech.endsWith('.')).toBe(true);
  });
});
