// Minimal jsdom bootstrap for interaction-level React tests run under
// `node --test` + tsx. Import this module for its side effect BEFORE rendering
// with react-dom/client; it installs the DOM globals React needs and flags the
// act() environment. renderToStaticMarkup tests are unaffected by a DOM being
// present, so this can coexist with the SSR tests in the same run.
import { JSDOM } from "jsdom";
import { after } from "node:test";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "http://localhost/",
  pretendToBeVisual: true,
});

const { window } = dom;

// Copy the DOM constructors/helpers React reads onto the Node global. navigator
// is a read-only getter on the Node global, so it is defined explicitly.
const globalAny = globalThis as unknown as Record<string, unknown>;
globalAny.window = window;
globalAny.document = window.document;
globalAny.HTMLElement = window.HTMLElement;
globalAny.Element = window.Element;
globalAny.Node = window.Node;
globalAny.Event = window.Event;
globalAny.CustomEvent = window.CustomEvent;
globalAny.MouseEvent = window.MouseEvent;
globalAny.getComputedStyle = window.getComputedStyle.bind(window);
globalAny.requestAnimationFrame = window.requestAnimationFrame.bind(window);
globalAny.cancelAnimationFrame = window.cancelAnimationFrame.bind(window);
// jsdom has no scrolling layout; overlay cleanup restores scroll in browsers.
window.scrollTo = () => {};
// next/link's intersection-observer fallback reads `self` at module scope and
// throws a ReferenceError without it.
globalAny.self = window;

// jsdom ships no matchMedia. Components that check prefers-reduced-motion
// before animating would throw on mount; report "no preference".
if (typeof window.matchMedia !== "function") {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
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
}
globalAny.matchMedia = window.matchMedia.bind(window);

Object.defineProperty(globalThis, "navigator", {
  value: window.navigator,
  configurable: true,
});

// React uses this flag to decide whether act() is required for updates.
globalAny.IS_REACT_ACT_ENVIRONMENT = true;

// Supabase uses Node's native BroadcastChannel when a DOM is present. Its
// MessagePort survives React unmounts and otherwise keeps the test worker alive.
const channels = new Set<BroadcastChannel>();
const NativeBroadcastChannel = globalThis.BroadcastChannel;
if (NativeBroadcastChannel) {
  globalAny.BroadcastChannel = class extends NativeBroadcastChannel {
    constructor(name: string) {
      super(name);
      channels.add(this);
    }
    close() {
      channels.delete(this);
      super.close();
    }
  };
}
after(() => {
  for (const channel of channels) channel.close();
  window.close();
});

export { window };
