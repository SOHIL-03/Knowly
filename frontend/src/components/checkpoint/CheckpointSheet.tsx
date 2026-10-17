import { useEffect, useRef, useState } from 'react';
import { AskBox } from '../AskBox';
import { Button } from '../ui';

export type CheckpointAction = 'deeper' | 'quiz' | 'save' | 'skip' | 'simpler' | 'apply' | 'lessLikeThis' | 'knewThis' | 'openSource';

const COUNTDOWN_SECONDS = 8;

/**
 * Checkpoint after each segment (sections 4.2 and 11.5): large actions, an Ask box that takes voice
 * or text, and an 8 second countdown that continues hands-free. Typing, speaking or tapping an action
 * pauses the countdown; tapping outside the sheet continues.
 */
export function CheckpointSheet({
  title,
  isLast,
  hasProject,
  onAsk,
  onAction,
  onContinue,
}: {
  title: string;
  isLast: boolean;
  hasProject: boolean;
  onAsk: (q: string) => void;
  onAction: (a: CheckpointAction) => void;
  onContinue: () => void;
}) {
  const [left, setLeft] = useState(COUNTDOWN_SECONDS);
  const [paused, setPaused] = useState(false);
  const continued = useRef(false);

  useEffect(() => {
    if (paused) return;
    if (left <= 0) {
      if (!continued.current) {
        continued.current = true;
        onContinue();
      }
      return;
    }
    const t = setTimeout(() => setLeft((l) => l - 1), 1000);
    return () => clearTimeout(t);
  }, [left, paused, onContinue]);

  const act = (a: CheckpointAction) => {
    setPaused(true);
    onAction(a);
  };

  const big = [
    ['Ask', null],
    ['Go deeper', 'deeper'],
    ['Quiz me', 'quiz'],
    ['Save', 'save'],
    ['Skip', 'skip'],
    ['Simpler', 'simpler'],
  ] as const;

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/30" onClick={onContinue} role="presentation">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="checkpoint-title"
        onClick={(e) => e.stopPropagation()}
        className="animate-sheet w-full max-w-xl rounded-t-[20px] border border-line bg-surface p-5 pb-[max(1.25rem,env(safe-area-inset-bottom))]"
      >
        <p className="text-caption text-ink-3">{title} · segment finished</p>
        <h2 id="checkpoint-title" className="mt-1 text-section font-semibold">
          Ask, go deeper or quiz?
        </h2>
        <div className="mt-3 h-1 overflow-hidden rounded-full bg-surface-2" aria-hidden>
          <div className="h-full bg-accent transition-[width] duration-1000 ease-linear" style={{ width: `${paused ? 100 : (left / COUNTDOWN_SECONDS) * 100}%` }} />
        </div>
        <p className="mt-1 text-caption text-ink-3" aria-live="polite">
          {paused ? 'Paused' : `${isLast ? 'Finishing' : 'Continuing'} in ${left} seconds`}
        </p>
        <div className="mt-4 grid grid-cols-2 gap-2">
          {big.map(([label, action]) =>
            action === null ? (
              <Button key={label} variant="secondary" onClick={() => setPaused(true)} className="min-h-14 bg-accent-soft">
                {label}
              </Button>
            ) : (
              <Button key={label} variant="secondary" onClick={() => act(action)} className="min-h-14">
                {label}
              </Button>
            ),
          )}
        </div>
        <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-caption">
          {hasProject && (
            <button className="min-h-9 text-accent underline-offset-2 hover:underline" onClick={() => act('apply')}>
              Apply to my project
            </button>
          )}
          <button className="min-h-9 text-ink-2 underline-offset-2 hover:underline" onClick={() => act('lessLikeThis')}>
            Less like this
          </button>
          <button className="min-h-9 text-ink-2 underline-offset-2 hover:underline" onClick={() => act('knewThis')}>
            Knew this already
          </button>
          <button className="min-h-9 text-ink-2 underline-offset-2 hover:underline" onClick={() => act('openSource')}>
            Open source
          </button>
        </div>
        <div className="mt-4">
          <AskBox
            placeholder="Type or speak a question…"
            onActivity={() => setPaused(true)}
            onSubmit={(q) => {
              setPaused(true);
              onAsk(q);
            }}
          />
        </div>
        <div className="mt-4 flex justify-end">
          <Button variant="ghost" onClick={onContinue}>
            {isLast ? 'Finish' : 'Continue'}
          </Button>
        </div>
      </div>
    </div>
  );
}
