import { useEffect, useRef, type ReactNode } from 'react';

const FOCUSABLE = 'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

let openCount = 0; // body scroll stays locked until the last modal closes

/**
 * Overlay + dialog shell shared by every demo modal. `center` modals sit in the middle of the screen and never scroll
 * (the page behind is locked too); `top` modals (player, quiz) start near the top and the overlay scrolls if needed.
 * Closes on Esc, traps Tab inside the dialog, and returns focus to the button that opened it.
 */
export function Modal({
  label, width, align = 'center', paddingTop, padding, onClose, children,
}: { label: string; width: number; align?: 'center' | 'top'; paddingTop?: number; padding?: string; onClose: () => void; children: ReactNode }) {
  const dialog = useRef<HTMLElement>(null);
  const close = useRef(onClose);
  close.current = onClose;

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    openCount += 1;
    document.body.style.overflow = 'hidden';
    const el = dialog.current;
    (el?.querySelector<HTMLElement>('[data-autofocus]') ?? el?.querySelector<HTMLElement>(FOCUSABLE) ?? el)?.focus();

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        close.current();
      } else if (e.key === 'Tab' && el) {
        const items = Array.from(el.querySelectorAll<HTMLElement>(FOCUSABLE));
        if (!items.length) return e.preventDefault();
        const first = items[0], last = items[items.length - 1];
        if (e.shiftKey && (document.activeElement === first || !el.contains(document.activeElement))) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && (document.activeElement === last || !el.contains(document.activeElement))) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      openCount -= 1;
      if (openCount === 0) document.body.style.overflow = '';
      if (opener && document.contains(opener)) opener.focus();
    };
  }, []);

  return (
    <div className={`overlay ${align}`} style={paddingTop ? { paddingTop } : undefined}>
      <section ref={dialog} role="dialog" aria-modal="true" aria-label={label} tabIndex={-1} className="card dialog" style={{ width, padding, overflow: 'hidden', outline: 'none' }}>
        {children}
      </section>
    </div>
  );
}

export function CloseIcon({ size = 20 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <path d="M18 6 6 18M6 6l12 12" />
    </svg>
  );
}
