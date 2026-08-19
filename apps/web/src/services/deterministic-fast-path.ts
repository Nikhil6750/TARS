/**
 * Trivial deterministic requests (arithmetic, unit-free numeric
 * expressions) never need to leave this process -- an LLM round trip for
 * "7 * 6" is both wasteful and, per tonight's own measurements, 3-30+
 * seconds slower than local evaluation. Returns the answer text if `text`
 * is confidently a pure arithmetic expression; null otherwise, meaning
 * "not deterministic, send it to the assistant as normal."
 *
 * Deliberately narrow: only unambiguous arithmetic. Anything with words
 * beyond an optional "what is/what's/calculate" prefix falls through to
 * the real assistant rather than risk a wrong "fast" answer.
 */

const QUESTION_PREFIX = /^(what'?s|what is|calculate|compute)\s+/i;
const TRAILING_QUESTION_MARK = /\?+\s*$/;

// Only digits, whitespace, and the operators/parens a 4-function
// calculator understands -- anything else means this isn't pure
// arithmetic and must fall through to the real assistant.
const ARITHMETIC_CHARS = /^[\d\s+\-*/x×÷().,]+$/;
const HAS_DIGIT = /\d/;
const HAS_OPERATOR = /[+\-*/x×÷]/;

export function tryDeterministicAnswer(rawText: string): string | null {
  let text = rawText.trim();
  if (!text) return null;

  text = text.replace(QUESTION_PREFIX, '').replace(TRAILING_QUESTION_MARK, '').trim();
  if (!text) return null;

  // Normalize spoken/typed multiplication and division symbols, and strip
  // thousands-separator commas, before validating character set.
  const normalized = text.replace(/x|×/gi, '*').replace(/÷/g, '/').replace(/,/g, '');

  if (!ARITHMETIC_CHARS.test(text.replace(/,/g, '')) || !HAS_DIGIT.test(normalized) || !HAS_OPERATOR.test(normalized)) {
    return null;
  }

  const result = evaluateArithmetic(normalized);
  if (result === null || !Number.isFinite(result)) return null;

  const formatted = Number.isInteger(result) ? String(result) : String(Math.round(result * 1e10) / 1e10);
  return formatted;
}

/** Minimal, safe recursive-descent arithmetic evaluator -- deliberately
 * NOT eval()/Function(): no identifiers, no property access, no code
 * execution, just +, -, *, /, parentheses, and numeric literals. */
function evaluateArithmetic(expr: string): number | null {
  let pos = 0;

  const peek = () => expr[pos];
  const skipSpace = () => {
    while (pos < expr.length && /\s/.test(expr[pos])) pos++;
  };

  function parseExpression(): number {
    let value = parseTerm();
    for (;;) {
      skipSpace();
      const op = peek();
      if (op === '+' || op === '-') {
        pos++;
        const rhs = parseTerm();
        value = op === '+' ? value + rhs : value - rhs;
      } else {
        break;
      }
    }
    return value;
  }

  function parseTerm(): number {
    let value = parseFactor();
    for (;;) {
      skipSpace();
      const op = peek();
      if (op === '*' || op === '/') {
        pos++;
        const rhs = parseFactor();
        if (op === '/') {
          if (rhs === 0) throw new Error('division by zero');
          value = value / rhs;
        } else {
          value = value * rhs;
        }
      } else {
        break;
      }
    }
    return value;
  }

  function parseFactor(): number {
    skipSpace();
    if (peek() === '-') {
      pos++;
      return -parseFactor();
    }
    if (peek() === '+') {
      pos++;
      return parseFactor();
    }
    if (peek() === '(') {
      pos++;
      const value = parseExpression();
      skipSpace();
      if (peek() !== ')') throw new Error('expected )');
      pos++;
      return value;
    }
    skipSpace();
    const start = pos;
    while (pos < expr.length && /[\d.]/.test(expr[pos])) pos++;
    if (pos === start) throw new Error('expected number');
    const num = Number(expr.slice(start, pos));
    if (Number.isNaN(num)) throw new Error('invalid number');
    return num;
  }

  try {
    const value = parseExpression();
    skipSpace();
    if (pos !== expr.length) return null; // trailing garbage -- not pure arithmetic
    return value;
  } catch {
    return null;
  }
}
