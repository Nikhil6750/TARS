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

    // Structured Trading Sections Formatter
    const upper = line.trim().toUpperCase();
    if (upper === 'STRUCTURE' || upper === '### STRUCTURE') {
      elements.push(
        <div key={index} className="flex items-center gap-1.5 mt-4 mb-1.5 font-semibold text-slate-900 text-xs uppercase tracking-wider">
          <span className="w-1.5 h-1.5 rounded-full bg-slate-700" />
          <span>Structure</span>
        </div>
      );
      return;
    }
    if (upper === 'BIAS' || upper.startsWith('BIAS:') || upper === '### BIAS') {
      const biasVal = line.replace(/#* BIAS:?/i, '').trim();
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
    if (upper === 'SETUP' || upper.startsWith('SETUP:') || upper === '### SETUP') {
      elements.push(
        <div key={index} className="flex items-center gap-1.5 mt-3 mb-1.5 font-semibold text-slate-900 text-xs uppercase tracking-wider">
          <span className="w-1.5 h-1.5 rounded-full bg-slate-700" />
          <span>Setup</span>
        </div>
      );
      return;
    }
    if (upper === 'KEY LEVELS' || upper === '### KEY LEVELS') {
      elements.push(
        <div key={index} className="flex items-center gap-1.5 mt-3 mb-1.5 font-semibold text-slate-900 text-xs uppercase tracking-wider">
          <span className="w-1.5 h-1.5 rounded-full bg-slate-700" />
          <span>Key Levels</span>
        </div>
      );
      return;
    }
    if (upper === 'RISK' || upper.startsWith('RISK:') || upper === '### RISK') {
      elements.push(
        <div key={index} className="flex items-center gap-1.5 mt-3 mb-1.5 font-semibold text-amber-700 text-xs uppercase tracking-wider">
          <ShieldAlert className="w-3.5 h-3.5 text-amber-600" />
          <span>Risk Warning</span>
        </div>
      );
      return;
    }
    if (upper === 'ACTION' || upper.startsWith('ACTION:') || upper === '### ACTION') {
      const actVal = line.replace(/#* ACTION:?/i, '').trim();
      elements.push(
        <div key={index} className="flex items-center gap-2 mt-3 mb-2 p-2 rounded-lg bg-slate-100 border border-slate-200">
          <span className="font-semibold text-slate-900 text-xs uppercase tracking-wider">Action:</span>
          <span className="text-xs text-slate-800 font-medium">{actVal || 'Watch & Wait'}</span>
        </div>
      );
      return;
    }

    // Headings
    if (line.startsWith('### ')) {
      elements.push(
        <h3 key={index} className="text-sm font-semibold text-slate-900 mt-3 mb-1.5">
          {line.slice(4)}
        </h3>
      );
      return;
    }
    if (line.startsWith('## ')) {
      elements.push(
        <h2 key={index} className="text-base font-semibold text-slate-900 mt-4 mb-2">
          {line.slice(3)}
        </h2>
      );
      return;
    }
    if (line.startsWith('# ')) {
      elements.push(
        <h1 key={index} className="text-lg font-bold text-slate-900 mt-4 mb-2">
          {line.slice(2)}
        </h1>
      );
      return;
    }

    // Bullet points
    if (line.trim().startsWith('- ') || line.trim().startsWith('* ')) {
      elements.push(
        <li key={index} className="ml-4 list-disc text-slate-800 my-0.5">
          {line.trim().slice(2)}
        </li>
      );
      return;
    }

    // Empty line / paragraph break
    if (!line.trim()) {
      elements.push(<div key={index} className="h-2" />);
      return;
    }

    // Standard paragraph line
    elements.push(
      <p key={index} className="my-0.5 leading-relaxed text-slate-800">
        {line}
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
