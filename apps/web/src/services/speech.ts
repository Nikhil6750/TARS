/**
 * Speech composition and sanitization utility for TARS TTS synthesis.
 * Converts structured assistant/chart display Markdown into a clean,
 * natural speech representation without raw asterisks, headings,
 * code blocks, URLs, or file paths.
 */

const URL_REGEX = /https?:\/\/\S+|www\.\S+/gi;
const WINDOWS_PATH_REGEX = /\b[A-Za-z]:\\[^\s]+/g;
const UNIX_PATH_REGEX = /(?<!\w)\/(?:[^\s/]+\/)+[^\s]+/g;

export function composeSpeech(displayText: string, limit: number = 600): string {
  if (!displayText) return '';

  let text = displayText.replace(/```[\s\S]*?```/g, ' Code example omitted. ');
  text = text.replace(/!\[([^\]]*)\]\([^)]+\)/g, '$1');
  text = text.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1');
  text = text.replace(URL_REGEX, '');
  text = text.replace(WINDOWS_PATH_REGEX, '');
  text = text.replace(UNIX_PATH_REGEX, '');
  text = text.replace(/^\s{0,3}(?:#{1,6}|[-*+] |\d+\. )\s*/gm, '');
  text = text.replace(/`([^`]+)`/g, '$1');
  text = text.replace(/\*\*([^*]+)\*\*|__([^_]+)__/g, (_m, p1, p2) => p1 || p2 || '');
  text = text.replace(/\*([^*]+)\*|_([^_]+)_/g, (_m, p1, p2) => p1 || p2 || '');
  text = text.replace(/[|#*_~]/g, ' ');
  text = text.replace(/\s+/g, ' ').trim();

  if (text.length > limit) {
    const truncated = text.slice(0, limit);
    const lastSpace = truncated.lastIndexOf(' ');
    const boundary = lastSpace > 0 ? truncated.slice(0, lastSpace) : truncated;
    text = boundary.replace(/[\s,;:]+$/, '') + '.';
  }

  return text;
}
