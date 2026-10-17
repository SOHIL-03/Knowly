import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

/** Minimal render helpers (no testing-library): mount into jsdom, click, type, and wait for async work. */
export function mount(ui: ReactElement) {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root: Root = createRoot(host);
  act(() => root.render(ui));
  return {
    host,
    unmount() {
      act(() => root.unmount());
      host.remove();
    },
  };
}

export const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });
export const buttons = () => Array.from(document.querySelectorAll('button'));
export const button = (label: string | RegExp) =>
  buttons().find((b) => (typeof label === 'string' ? b.textContent?.trim() === label || b.getAttribute('aria-label') === label : label.test(b.textContent ?? '') || label.test(b.getAttribute('aria-label') ?? '')))!;
export const click = (el: Element) => act(async () => { el.dispatchEvent(new MouseEvent('click', { bubbles: true })); });

export function type(el: HTMLTextAreaElement | HTMLInputElement, value: string) {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  return act(async () => {
    Object.getOwnPropertyDescriptor(proto, 'value')!.set!.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

export const textarea = (label?: string) =>
  (label ? document.querySelector(`textarea[aria-label="${label}"]`) : document.querySelector('textarea')) as HTMLTextAreaElement | null;
