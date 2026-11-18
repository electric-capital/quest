// The update arrival animation: duration scaled by distance within
// [ARRIVAL_MIN_MS, ARRIVAL_MAX_MS], no height animation past the tall-result
// cap, nothing moving under prefers-reduced-motion (only the highlight), and
// no-ops where Web Animations is missing. jsdom has no layout or Web
// Animations, so `animate`, the measured height and matchMedia are stubbed;
// what the keyframes look like on screen is not tested here.
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  ARRIVAL_MAX_HEIGHT_PX,
  ARRIVAL_MAX_MS,
  ARRIVAL_MIN_MS,
  arrivalDuration,
  playArrival,
  prefersReducedMotion,
} from './autoprojectArrival';

function stubReducedMotion(reduce: boolean | undefined) {
  if (reduce === undefined) {
    vi.stubGlobal('matchMedia', undefined);
    return;
  }
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: reduce && query === '(prefers-reduced-motion: reduce)',
    media: query,
  }));
}

interface FakeAnimation {
  keyframes: Keyframe[];
  options: KeyframeAnimationOptions;
  listeners: Record<string, () => void>;
}

/** An element of `height` px with a recording `animate`. */
function element(height: number) {
  const el = document.createElement('div');
  el.getBoundingClientRect = () => ({ height } as DOMRect);
  const animations: FakeAnimation[] = [];
  el.animate = ((keyframes: Keyframe[], options: KeyframeAnimationOptions) => {
    const anim: FakeAnimation = { keyframes, options, listeners: {} };
    animations.push(anim);
    return {
      addEventListener: (type: string, fn: () => void) => { anim.listeners[type] = fn; },
    } as unknown as Animation;
  }) as HTMLElement['animate'];
  return { el, animations };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('arrivalDuration', () => {
  it.each([
    [0, ARRIVAL_MIN_MS],
    [100, ARRIVAL_MIN_MS + 30],
    [-100, ARRIVAL_MIN_MS + 30],
    [10_000, ARRIVAL_MAX_MS],
  ])('%i px -> %i ms', (distance, expected) => {
    expect(arrivalDuration(distance)).toBe(expected);
  });

  it('grows with distance and stays within the bounds', () => {
    let last = 0;
    for (let d = 0; d <= 1_000; d += 50) {
      const ms = arrivalDuration(d);
      expect(ms).toBeGreaterThanOrEqual(last);
      expect(ms).toBeGreaterThanOrEqual(ARRIVAL_MIN_MS);
      expect(ms).toBeLessThanOrEqual(ARRIVAL_MAX_MS);
      last = ms;
    }
  });
});

describe('prefersReducedMotion', () => {
  it.each([[true, true], [false, false], [undefined, false]])('matchMedia %s -> %s', (reduce, expected) => {
    stubReducedMotion(reduce);
    expect(prefersReducedMotion()).toBe(expected);
  });
});

describe('playArrival', () => {
  it('opens the height from 0 and fades the content in', () => {
    stubReducedMotion(false);
    const { el, animations } = element(200);
    playArrival(el);
    expect(animations).toHaveLength(2);
    const [grow, fade] = animations;
    expect(grow.keyframes).toEqual([{ height: '0px' }, { height: '200px' }]);
    expect(grow.options.duration).toBe(arrivalDuration(200));
    expect(fade.keyframes[0]).toMatchObject({ opacity: 0 });
    expect(fade.keyframes[1]).toMatchObject({ opacity: 1 });
    // Overflow is clipped while the height animates and restored after.
    expect(el.style.overflow).toBe('hidden');
    grow.listeners.finish();
    expect(el.style.overflow).toBe('');
  });

  it('grows from the replaced activity line height', () => {
    stubReducedMotion(false);
    const { el, animations } = element(300);
    playArrival(el, { fromHeight: 40 });
    expect(animations[0].keyframes).toEqual([{ height: '40px' }, { height: '300px' }]);
    expect(animations[0].options.duration).toBe(arrivalDuration(260));
  });

  it('only fades a result taller than the cap', () => {
    stubReducedMotion(false);
    const { el, animations } = element(ARRIVAL_MAX_HEIGHT_PX + 1);
    playArrival(el);
    expect(animations).toHaveLength(1);
    expect(animations[0].keyframes[0]).toMatchObject({ opacity: 0 });
    expect(el.style.overflow).toBe('');
  });

  it('does not animate the height when nothing changes size', () => {
    stubReducedMotion(false);
    const { el, animations } = element(50);
    playArrival(el, { fromHeight: 50 });
    expect(animations).toHaveLength(1);
  });

  it('moves nothing under reduced motion, but still highlights', () => {
    stubReducedMotion(true);
    const { el, animations } = element(200);
    playArrival(el, { highlight: true });
    expect(animations).toEqual([]);
    expect(el.classList.contains('autoproject-arrived')).toBe(true);
    el.dispatchEvent(new Event('animationend'));
    expect(el.classList.contains('autoproject-arrived')).toBe(false);
  });

  it('highlights a completed result after the motion', () => {
    stubReducedMotion(false);
    const { el, animations } = element(100);
    playArrival(el, { highlight: true });
    expect(animations).toHaveLength(2);
    expect(el.classList.contains('autoproject-arrived')).toBe(true);
  });

  it('is a no-op without Web Animations and without a highlight', () => {
    stubReducedMotion(false);
    const el = document.createElement('div');
    Object.defineProperty(el, 'animate', { value: undefined });
    expect(() => playArrival(el)).not.toThrow();
    expect(el.classList.contains('autoproject-arrived')).toBe(false);
  });
});
