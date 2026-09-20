import React, { useState } from 'react';
import { Copy, Check, TrendingUp, TrendingDown, Minus, ShieldAlert } from 'lucide-react';

interface MarkdownContentProps {
  content: string;
  isStreaming?: boolean;
}

function renderCodeBlock(code: string, language: string, key: number) {
  return <CodeBlock key={key} code={code} language={language} />;
}

const CodeBlock: React.FC<{ code: string; language: string }> = ({ code, language }) => {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="my-3 rounded-xl overflow-hidden border border-slate-200 bg-slate-900 text-slate-100 font-mono text-xs shadow-xs">
      <div className="flex items-center justify-between px-3.5 py-1.5 bg-slate-800/90 text-slate-300 text-[11px] border-b border-slate-700/60">
        <span className="font-sans font-medium lowercase text-slate-300">{language || 'text'}</span>
        <button
          type="button"
          onClick={handleCopy}
          className="flex items-center gap-1 hover:text-white transition-colors cursor-pointer"
        >
          {copied ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
          <span>{copied ? 'Copied' : 'Copy'}</span>
        </button>
      </div>
      <pre className="p-3.5 overflow-x-auto custom-scrollbar leading-relaxed">
        <code>{code}</code>
      </pre>
    </div>
  );
};

function renderInline(text: string, isStreaming: boolean = false): React.ReactNode[] {
  let processed = text;
  if (isStreaming) {
    if (processed.endsWith('**') || processed.endsWith('__')) {
      processed = processed.slice(0, -2);
    } else if (processed.endsWith('`') || processed.endsWith('*') || processed.endsWith('_')) {
      processed = processed.slice(0, -1);
    }
  }

  const regex = /(`[^`]+`|\*\*[^*]+\*\*|__[^_]+__|\*[^*]+\*|_[^_]+_|\[[^\]]+\]\([^)]+\))/g;
  const parts = processed.split(regex);
  return parts.map((part, i) => {
    if (!part) return null;
    if (part.startsWith('`') && part.endsWith('`') && part.length >= 2) {
      return (
        <code key={i} className="px-1 py-0.5 mx-0.5 rounded bg-slate-100 font-mono text-xs text-slate-800 border border-slate-200">
          {part.slice(1, -1)}
        </code>
      );
    }
    if (
      (part.startsWith('**') && part.endsWith('**') && part.length >= 4) ||
      (part.startsWith('__') && part.endsWith('__') && part.length >= 4)
    ) {
      return (
        <strong key={i} className="font-semibold text-slate-900">
          {part.slice(2, -2)}
        </strong>
      );
    }
    if (
      (part.startsWith('*') && part.endsWith('*') && part.length >= 2) ||
      (part.startsWith('_') && part.endsWith('_') && part.length >= 2)
    ) {
      return (
        <em key={i} className="italic text-slate-800">
          {part.slice(1, -1)}
        </em>
      );
    }
    const linkMatch = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
    if (linkMatch) {
      return (
        <a key={i} href={linkMatch[2]} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline">
          {linkMatch[1]}
        </a>
      );
    }
    if (isStreaming && (part.startsWith('**') || part.startsWith('__')) && part.length > 2) {
      return (
        <strong key={i} className="font-semibold text-slate-900">
          {part.slice(2)}
        </strong>
      );
    }
    if (isStreaming && (part.startsWith('*') || part.startsWith('_')) && part.length > 1) {
      return (
        <em key={i} className="italic text-slate-800">
          {part.slice(1)}
        </em>
      );
    }
    return part;
  });
}

export const MarkdownContent: React.FC<MarkdownContentProps> = ({ content, isStreaming = false }) => {
  if (!content) return null;

  const lines = content.split('\n');
  const elements: React.ReactNode[] = [];
  let inCodeBlock = false;
  let codeBuffer: string[] = [];
  let codeLang = '';

  lines.forEach((line, index) => {
    // Code block toggle
    if (line.trim().startsWith('```')) {
      if (inCodeBlock) {
        elements.push(renderCodeBlock(codeBuffer.join('\n'), codeLang, index));
        codeBuffer = [];
        inCodeBlock = false;
      } else {
        inCodeBlock = true;
        codeLang = line.trim().slice(3).trim();
      }
      return;
    }

    if (inCodeBlock) {
      codeBuffer.push(line);
      return;
    }

    const trimmed = line.trim();
    const cleanHeader = trimmed
      .replace(/^\*\*|\*\*$/g, '')
      .replace(/^#{1,6}\s*/, '')
      .replace(/:$/, '')
      .trim()
      .toUpperCase();

    // Structured Trading Sections Formatter
    if (cleanHeader === 'STRUCTURE') {
      elements.push(
        <div key={index} className="flex items-center gap-1.5 mt-4 mb-1.5 font-semibold text-slate-900 text-xs uppercase tracking-wider">
          <span className="w-1.5 h-1.5 rounded-full bg-slate-700" />
          <span>Structure</span>
        </div>
      );
      return;
    }
    if (cleanHeader === 'WHAT I SEE') {
      elements.push(
        <div key={index} className="flex items-center gap-1.5 mt-4 mb-1.5 font-semibold text-slate-900 text-xs uppercase tracking-wider">
          <span className="w-1.5 h-1.5 rounded-full bg-indigo-600" />
          <span>What I See</span>
        </div>
      );
      return;
    }
    if (cleanHeader === 'MARKET STATE' || cleanHeader === 'MARKET STATE & STRUCTURE') {
      elements.push(
        <div key={index} className="flex items-center gap-1.5 mt-4 mb-1.5 font-semibold text-slate-900 text-xs uppercase tracking-wider">
          <span className="w-1.5 h-1.5 rounded-full bg-blue-600" />
          <span>Market State</span>
        </div>
      );
      return;
    }
    if (cleanHeader === 'BULLISH SCENARIO') {
      elements.push(
        <div key={index} className="flex items-center gap-1.5 mt-3 mb-1.5 font-semibold text-emerald-700 text-xs uppercase tracking-wider">
          <TrendingUp className="w-3.5 h-3.5 text-emerald-600" />
          <span>Bullish Scenario</span>
        </div>
      );
      return;
    }
    if (cleanHeader === 'BEARISH SCENARIO') {
      elements.push(
        <div key={index} className="flex items-center gap-1.5 mt-3 mb-1.5 font-semibold text-rose-700 text-xs uppercase tracking-wider">
          <TrendingDown className="w-3.5 h-3.5 text-rose-600" />
          <span>Bearish Scenario</span>
        </div>
      );
      return;
    }
    if (cleanHeader === 'BIAS' || trimmed.toUpperCase().startsWith('BIAS:')) {
      const biasVal = trimmed.replace(/^#*\s*BIAS:?/i, '').replace(/^\*\*|\*\*$/g, '').trim();
      const isBull = /bull/i.test(biasVal);
      const isBear = /bear/i.test(biasVal);
      elements.push(
        <div key={index} className="flex items-center gap-2 mt-3 mb-1.5">
          <span className="font-semibold text-slate-900 text-xs uppercase tracking-wider">Bias:</span>
          <span
            className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs font-semibold ${
              isBull
                ? 'bg-emerald-50 text-emerald-700 border border-emerald-200'
                : isBear
                ? 'bg-rose-50 text-rose-700 border border-rose-200'
                : 'bg-slate-100 text-slate-700 border border-slate-200'
            }`}
          >
            {isBull ? <TrendingUp className="w-3 h-3" /> : isBear ? <TrendingDown className="w-3 h-3" /> : <Minus className="w-3 h-3" />}
            {biasVal || 'Neutral'}
          </span>
        </div>
      );
      return;
    }
    if (cleanHeader === 'SETUP' || cleanHeader === 'TRADE STATUS' || trimmed.toUpperCase().startsWith('SETUP:')) {
      elements.push(
        <div key={index} className="flex items-center gap-1.5 mt-3 mb-1.5 font-semibold text-slate-900 text-xs uppercase tracking-wider">
          <span className="w-1.5 h-1.5 rounded-full bg-slate-700" />
          <span>{cleanHeader === 'TRADE STATUS' ? 'Trade Status' : 'Setup'}</span>
        </div>
      );
      return;
    }
    if (cleanHeader === 'KEY LEVELS') {
      elements.push(
        <div key={index} className="flex items-center gap-1.5 mt-3 mb-1.5 font-semibold text-slate-900 text-xs uppercase tracking-wider">
          <span className="w-1.5 h-1.5 rounded-full bg-slate-700" />
          <span>Key Levels</span>
        </div>
      );
      return;
    }
    if (cleanHeader === 'INVALIDATION' || trimmed.toUpperCase().startsWith('INVALIDATION:')) {
      elements.push(
        <div key={index} className="flex items-center gap-1.5 mt-3 mb-1.5 font-semibold text-amber-700 text-xs uppercase tracking-wider">
          <span className="w-1.5 h-1.5 rounded-full bg-amber-600" />
          <span>Invalidation</span>
        </div>
      );
      return;
    }
    if (cleanHeader === 'RISK' || cleanHeader === 'RISK WARNING' || cleanHeader === 'RISK FACTORS' || trimmed.toUpperCase().startsWith('RISK:')) {
      elements.push(
        <div key={index} className="flex items-center gap-1.5 mt-3 mb-1.5 font-semibold text-amber-700 text-xs uppercase tracking-wider">
          <ShieldAlert className="w-3.5 h-3.5 text-amber-600" />
          <span>Risk Warning</span>
        </div>
      );
      return;
    }
    if (cleanHeader === 'ACTION' || trimmed.toUpperCase().startsWith('ACTION:')) {
      const actVal = trimmed.replace(/^#*\s*ACTION:?/i, '').replace(/^\*\*|\*\*$/g, '').trim();
      elements.push(
        <div key={index} className="flex items-center gap-2 mt-3 mb-2 p-2 rounded-lg bg-slate-100 border border-slate-200">
          <span className="font-semibold text-slate-900 text-xs uppercase tracking-wider">Action:</span>
          <span className="text-xs text-slate-800 font-medium">{actVal || 'Watch & Wait'}</span>
        </div>
      );
      return;
    }

    // Standard Headings
    if (line.startsWith('### ')) {
      elements.push(
        <h3 key={index} className="text-sm font-semibold text-slate-900 mt-3 mb-1.5">
          {renderInline(line.slice(4))}
        </h3>
      );
      return;
    }
    if (line.startsWith('## ')) {
      elements.push(
        <h2 key={index} className="text-base font-semibold text-slate-900 mt-4 mb-2">
          {renderInline(line.slice(3))}
        </h2>
      );
      return;
    }
    if (line.startsWith('# ')) {
      elements.push(
        <h1 key={index} className="text-lg font-bold text-slate-900 mt-4 mb-2">
          {renderInline(line.slice(2))}
        </h1>
      );
      return;
    }

    // Bullet points (-, *, •)
    if (trimmed.startsWith('- ') || trimmed.startsWith('* ') || trimmed.startsWith('• ')) {
      elements.push(
        <li key={index} className="ml-4 list-disc text-slate-800 my-0.5">
          {renderInline(trimmed.slice(2))}
        </li>
      );
      return;
    }

    // Empty line / paragraph break
    if (!trimmed) {
      elements.push(<div key={index} className="h-2" />);
      return;
    }

    // Standard paragraph line with full inline formatting
    elements.push(
      <p key={index} className="my-0.5 leading-relaxed text-slate-800">
        {renderInline(line)}
      </p>
    );
  });

  if (inCodeBlock && codeBuffer.length > 0) {
    elements.push(renderCodeBlock(codeBuffer.join('\n'), codeLang, lines.length));
  }

  return (
    <div className="space-y-0.5">
      {elements}
      {isStreaming && (
        <span className="inline-block w-1.5 h-3.5 bg-slate-900 animate-pulse ml-0.5 align-middle" />
      )}
    </div>
  );
};
