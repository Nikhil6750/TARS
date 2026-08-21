import { describe, it, expect, vi, beforeEach } from 'vitest';

describe('Single-Utterance & Two-Stage Wake Canonical Dispatching', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('single-utterance "Hey TARS, analyze the chart" produces exactly ONE dispatch', () => {
    const onWake = vi.fn();
    const onChart = vi.fn();
    const onCommand = vi.fn();

    // Simulate native wake engine dispatch logic for single-utterance
    const WAKE_REGEX = /\b(hey[\s,]+tars|tars|hey[\s,]+tar|ok[\s,]+tars|hey[\s,]+torres|hi[\s,]+tars)\b/i;
    const ANALYZE_CHART_REGEX =
      /\b(analy[sz]e|check|look\s+at|evaluate|read|scan|inspect|review|what\s+do\s+you\s+see\s+on)[\s,]+(?:this|the|my|active|current)?\s*charts?\b/i;

    function simulateNativeUtteranceDispatch(transcript: string) {
      const wakeMatch = WAKE_REGEX.exec(transcript);
      if (wakeMatch) {
        const tail = transcript.slice(wakeMatch.index + wakeMatch[0].length).trim();
        const cleanTail = tail.replace(/^[\s,:-]+/, '').trim();

        if (cleanTail.length > 0) {
          // SINGLE-UTTERANCE: exactly ONE event emitted
          if (ANALYZE_CHART_REGEX.test(cleanTail)) {
            onChart(cleanTail);
          } else {
            onCommand(cleanTail);
          }
          return;
        }

        // TWO-STAGE: wake detected only
        onWake(transcript);
        return;
      }

      if (ANALYZE_CHART_REGEX.test(transcript)) {
        onChart(transcript);
      }
    }

    simulateNativeUtteranceDispatch('Hey TARS, analyze the chart');

    expect(onChart).toHaveBeenCalledTimes(1);
    expect(onChart).toHaveBeenCalledWith('analyze the chart');
    expect(onWake).not.toHaveBeenCalled();
    expect(onCommand).not.toHaveBeenCalled();
  });

  it('single-utterance "Hey TARS, what is the risk on Gold?" produces exactly ONE command-transcript dispatch', () => {
    const onWake = vi.fn();
    const onChart = vi.fn();
    const onCommand = vi.fn();

    const WAKE_REGEX = /\b(hey[\s,]+tars|tars|hey[\s,]+tar|ok[\s,]+tars|hey[\s,]+torres|hi[\s,]+tars)\b/i;
    const ANALYZE_CHART_REGEX =
      /\b(analy[sz]e|check|look\s+at|evaluate|read|scan|inspect|review|what\s+do\s+you\s+see\s+on)[\s,]+(?:this|the|my|active|current)?\s*charts?\b/i;

    function simulateNativeUtteranceDispatch(transcript: string) {
      const wakeMatch = WAKE_REGEX.exec(transcript);
      if (wakeMatch) {
        const tail = transcript.slice(wakeMatch.index + wakeMatch[0].length).trim();
        const cleanTail = tail.replace(/^[\s,:-]+/, '').trim();

        if (cleanTail.length > 0) {
          if (ANALYZE_CHART_REGEX.test(cleanTail)) {
            onChart(cleanTail);
          } else {
            onCommand(cleanTail);
          }
          return;
        }

        onWake(transcript);
        return;
      }
    }

    simulateNativeUtteranceDispatch('Hey TARS, what is the risk on Gold?');

    expect(onCommand).toHaveBeenCalledTimes(1);
    expect(onCommand).toHaveBeenCalledWith('what is the risk on Gold?');
    expect(onWake).not.toHaveBeenCalled();
    expect(onChart).not.toHaveBeenCalled();
  });

  it('two-stage "Hey TARS" followed by pause and "Analyze the chart" dispatches correctly in two distinct stages', () => {
    const onWake = vi.fn();
    const onChart = vi.fn();
    const onCommand = vi.fn();

    const WAKE_REGEX = /\b(hey[\s,]+tars|tars|hey[\s,]+tar|ok[\s,]+tars|hey[\s,]+torres|hi[\s,]+tars)\b/i;
    const ANALYZE_CHART_REGEX =
      /\b(analy[sz]e|check|look\s+at|evaluate|read|scan|inspect|review|what\s+do\s+you\s+see\s+on)[\s,]+(?:this|the|my|active|current)?\s*charts?\b/i;

    let inCommandListening = false;

    function simulateUtterance(transcript: string) {
      if (inCommandListening) {
        inCommandListening = false;
        if (ANALYZE_CHART_REGEX.test(transcript)) {
          onChart(transcript);
        } else {
          onCommand(transcript);
        }
        return;
      }

      const wakeMatch = WAKE_REGEX.exec(transcript);
      if (wakeMatch) {
        const tail = transcript.slice(wakeMatch.index + wakeMatch[0].length).trim();
        const cleanTail = tail.replace(/^[\s,:-]+/, '').trim();
        if (cleanTail.length > 0) {
          if (ANALYZE_CHART_REGEX.test(cleanTail)) {
            onChart(cleanTail);
          } else {
            onCommand(cleanTail);
          }
          return;
        }

        inCommandListening = true;
        onWake(transcript);
      }
    }

    // First utterance: user says "Hey TARS"
    simulateUtterance('Hey TARS');
    expect(onWake).toHaveBeenCalledTimes(1);
    expect(onChart).not.toHaveBeenCalled();
    expect(inCommandListening).toBe(true);

    // Second utterance: user says "Analyze the chart"
    simulateUtterance('Analyze the chart');
    expect(onChart).toHaveBeenCalledTimes(1);
    expect(onChart).toHaveBeenCalledWith('Analyze the chart');
    expect(inCommandListening).toBe(false);
  });
});
