/**
 * One thing speaks at a time. Whoever starts playing claims the focus and whatever played before is paused:
 * a spoken answer interrupts the podcast, and a newer answer (or the podcast itself) interrupts an older answer.
 */
let current: HTMLAudioElement | null = null;

export function claimAudio(el: HTMLAudioElement): void {
  if (current && current !== el && !current.paused) current.pause();
  current = el;
}

export function releaseAudio(el: HTMLAudioElement): void {
  if (current === el) current = null;
}

/** Plays `el`; resolves false when the browser blocks autoplay (the caller then shows a play button). */
export async function playExclusive(el: HTMLAudioElement): Promise<boolean> {
  claimAudio(el);
  try {
    await el.play();
    return true;
  } catch {
    return false;
  }
}

/** Stops whatever is speaking (used when a new question is sent). */
export function pauseCurrent(): void {
  if (current && !current.paused) current.pause();
}
