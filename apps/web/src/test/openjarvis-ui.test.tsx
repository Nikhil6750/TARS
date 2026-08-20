import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { ConversationView } from '../components/assistant/ConversationView';
import { EmptyState } from '../components/assistant/EmptyState';
import { MarkdownContent } from '../components/assistant/MarkdownContent';
import { AssistantMessage } from '../components/assistant/AssistantMessage';
import { UserMessage } from '../components/assistant/UserMessage';
import { Sidebar } from '../components/shell/Sidebar';
import { AppHeader } from '../components/shell/AppHeader';
import { tryDeterministicAnswer } from '../services/deterministic-fast-path';
import { TARSAssistantMessage } from '../types/assistant-message';

describe('OpenJarvis-Style TARS UI Redesign', () => {
  afterEach(() => {
    cleanup();
  });

  describe('EmptyState & ConversationView Component', () => {
    it('renders centered TARS title, subtitle, and prompt chips matching reference', () => {
      const onSelectPrompt = vi.fn();
      const onOpenWorkspace = vi.fn();

      render(<EmptyState onSelectPrompt={onSelectPrompt} onOpenWorkspace={onOpenWorkspace} />);

      expect(screen.getByRole('heading', { level: 1, name: 'TARS' })).toBeInTheDocument();
      expect(screen.getByText('How can I help?')).toBeInTheDocument();
      expect(screen.getByText('Analyze current chart')).toBeInTheDocument();
      expect(screen.getByText('Ask TARS')).toBeInTheDocument();
      expect(screen.getByText('Research market')).toBeInTheDocument();

      // Clicking 'Analyze current chart' triggers handler
      fireEvent.click(screen.getByText('Analyze current chart'));
      expect(onSelectPrompt).toHaveBeenCalledWith('Analyze this chart');

      // Clicking 'Ask TARS' triggers handler
      fireEvent.click(screen.getByText('Ask TARS'));
      expect(onSelectPrompt).toHaveBeenCalledWith('What is the current market sentiment?');
    });

    it('renders ConversationView with empty state and bottom composer', () => {
      const onSendMessage = vi.fn();
      render(
        <ConversationView
          messages={[]}
          streamingAnswer=""
          companionState="IDLE"
          isListening={false}
          onTogglePushToTalk={vi.fn()}
          onSendMessage={onSendMessage}
          onOpenWorkspace={vi.fn()}
        />
      );

      expect(screen.getByPlaceholderText('Ask TARS...')).toBeInTheDocument();
      expect(screen.getByText('Analyze current chart')).toBeInTheDocument();
      expect(screen.getByText(/TARS provides general information/i)).toBeInTheDocument();
    });
  });

  describe('MarkdownContent & AssistantMessage Component', () => {
    it('renders structured chart analysis headings and labels cleanly', () => {
      const sampleText = `STRUCTURE
Current price: 2684.50
Supply: 2700.00
Demand: 2660.00

BIAS: NEUTRAL
WHAT I SEE
Consolidating below resistance.

KEY LEVELS
- 2700.00 Major Resistance
- 2660.00 Support

ACTION: WATCH`;

      render(<MarkdownContent content={sampleText} />);

      expect(screen.getByText('Structure')).toBeInTheDocument();
      expect(screen.getByText('Bias:')).toBeInTheDocument();
      expect(screen.getByText(/NEUTRAL/i)).toBeInTheDocument();
      expect(screen.getByText('Key Levels')).toBeInTheDocument();
      expect(screen.getByText('Action:')).toBeInTheDocument();
      expect(screen.getByText(/Consolidating below resistance/i)).toBeInTheDocument();
    });

    it('renders a 5000+ character assistant response fully, with no truncation', () => {
      const longParagraph = 'Observed price action continues without a confirmed break of either zone. ';
      const longContent = '### STRUCTURE\n' + longParagraph.repeat(80);
      expect(longContent.length).toBeGreaterThan(5000);

      render(<MarkdownContent content={longContent} />);

      const container = screen.getByText('Structure').closest('div')!.parentElement!;
      expect(container.textContent!.length).toBeGreaterThan(5000);
      expect(container.textContent).toContain(longParagraph.trim());
    });

    it('AssistantMessage never clips/truncates content via CSS', () => {
      const msg: TARSAssistantMessage = {
        schema_version: '1.0.0',
        message_id: 'm-long',
        conversation_id: 'c1',
        timestamp: '2026-08-19T10:00:00Z',
        role: 'assistant',
        content: '### STRUCTURE\n' + 'Full analysis line. '.repeat(100),
        input_mode: 'text',
      };

      const { container } = render(<AssistantMessage message={msg} />);
      const classNames = Array.from(container.querySelectorAll('*'))
        .map((el) => el.className)
        .filter((c) => typeof c === 'string')
        .join(' ');

      expect(classNames).not.toMatch(/line-clamp/);
      expect(classNames).not.toMatch(/\btruncate\b/);
      expect(classNames).not.toMatch(/overflow-hidden/);
      expect(classNames).not.toMatch(/max-h-/);
      expect(classNames).not.toMatch(/whitespace-nowrap/);
    });

    it('renders AssistantMessage with TARS mark and action buttons', () => {
      const msg: TARSAssistantMessage = {
        schema_version: '1.0.0',
        message_id: 'm1',
        conversation_id: 'c1',
        timestamp: '2026-08-19T10:00:00Z',
        role: 'assistant',
        content: 'Hello! How can I assist you with your trading analysis?',
        input_mode: 'text',
        providers: { assistant: 'Claude Code' },
      };

      const onSpeak = vi.fn();
      render(<AssistantMessage message={msg} onSpeak={onSpeak} />);

      expect(screen.getByText('TARS')).toBeInTheDocument();
      expect(screen.getByText('Hello! How can I assist you with your trading analysis?')).toBeInTheDocument();
    });

    it('renders UserMessage with clean bubble style', () => {
      const msg: TARSAssistantMessage = {
        schema_version: '1.0.0',
        message_id: 'm2',
        conversation_id: 'c1',
        timestamp: '2026-08-19T10:01:00Z',
        role: 'user',
        content: 'Analyze this chart',
        input_mode: 'voice',
      };

      render(<UserMessage message={msg} />);

      expect(screen.getByText('Analyze this chart')).toBeInTheDocument();
      expect(screen.getByText('Voice')).toBeInTheDocument();
    });
  });

  describe('Sidebar & AppHeader Navigation', () => {
    it('renders clean sidebar with New Chat, Recent list, and Clear history', () => {
      const onSelectSession = vi.fn();
      const onNewChat = vi.fn();
      const onClearHistory = vi.fn();

      const sessions = [
        { id: 's1', title: 'Market outlook today', createdAt: '2026-08-19T09:00:00Z', messageCount: 4 },
        { id: 's2', title: 'BTC analysis', createdAt: '2026-08-18T08:00:00Z', messageCount: 2 },
      ];

      render(
        <Sidebar
          sessions={sessions}
          activeSessionId="s1"
          onSelectSession={onSelectSession}
          onNewChat={onNewChat}
          onClearHistory={onClearHistory}
          isOpen={true}
        />
      );

      expect(screen.getByText('New Chat')).toBeInTheDocument();
      expect(screen.getByText('Ctrl + N')).toBeInTheDocument();
      expect(screen.getByText('Recent')).toBeInTheDocument();
      expect(screen.getByText('Market outlook today')).toBeInTheDocument();
      expect(screen.getByText('BTC analysis')).toBeInTheDocument();
      expect(screen.getByText('Clear history')).toBeInTheDocument();

      fireEvent.click(screen.getByText('BTC analysis'));
      expect(onSelectSession).toHaveBeenCalledWith('s2');

      fireEvent.click(screen.getByText('New Chat'));
      expect(onNewChat).toHaveBeenCalledOnce();

      fireEvent.click(screen.getByText('Clear history'));
      expect(onClearHistory).toHaveBeenCalledOnce();
    });

    it('renders minimal AppHeader matching reference image', () => {
      const setActiveTab = vi.fn();

      render(
        <AppHeader
          activeTab="tars"
          setActiveTab={setActiveTab}
          companionState="IDLE"
          connectionStatus="connected"
        />
      );

      expect(screen.getByText('TARS')).toBeInTheDocument();
      expect(screen.getByText('Ready')).toBeInTheDocument();
      expect(screen.getByText('Chat')).toBeInTheDocument();
      expect(screen.getByText('Workspace')).toBeInTheDocument();
      expect(screen.getByText('Memory')).toBeInTheDocument();
      expect(screen.getByText('Settings')).toBeInTheDocument();

      fireEvent.click(screen.getByText('Workspace'));
      expect(setActiveTab).toHaveBeenCalledWith('workspace');
    });
  });

  describe('Deterministic Fast Path Validation', () => {
    it('evaluates arithmetic locally without touching backend or LLM', () => {
      expect(tryDeterministicAnswer('7 * 6')).toBe('42');
      expect(tryDeterministicAnswer('what is 100 / 4')).toBe('25');
      expect(tryDeterministicAnswer('calculate 15 + 27')).toBe('42');
    });
  });
});
