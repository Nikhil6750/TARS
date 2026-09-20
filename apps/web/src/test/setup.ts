import '@testing-library/jest-dom';

// Polyfill window.matchMedia
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});

// Polyfill scrollIntoView
Element.prototype.scrollIntoView = () => {};

// Polyfill Canvas 2D context for jsdom
(HTMLCanvasElement.prototype as unknown as { getContext: unknown }).getContext = () => ({
  clearRect: () => {},
  beginPath: () => {},
  roundRect: () => {},
  fill: () => {},
  createLinearGradient: () => ({
    addColorStop: () => {},
  }),
});

// Mock SpeechSynthesis
if (typeof window !== 'undefined') {
  (window as unknown as { speechSynthesis: unknown }).speechSynthesis = {
    speak: () => {},
    cancel: () => {},
    pause: () => {},
    resume: () => {},
    getVoices: () => [],
  };
}
