// PCM helpers for live dictation: resampling, int16 packing, frame batching.
import { describe, expect, it } from 'vitest';
import { FrameBatcher, floatToPcm16, resample, rms } from './pcm';

describe('resample', () => {
  it('returns the input untouched at the same rate', () => {
    const input = new Float32Array([0.1, 0.2, 0.3]);
    expect(resample(input, 16000, 16000)).toBe(input);
  });

  it('averages groups for an integer downsampling ratio', () => {
    const input = new Float32Array([0, 0.3, 0.6, 0.9, 0.9, 0.9, 0.5]);
    const out = resample(input, 48000, 16000);
    expect(Array.from(out)).toHaveLength(2);
    expect(out[0]).toBeCloseTo(0.3, 5);
    expect(out[1]).toBeCloseTo(0.9, 5);
  });

  it('interpolates linearly for a non-integer ratio', () => {
    const input = new Float32Array([0, 1, 2, 3]);
    const out = resample(input, 44100, 16000);
    // 4 samples at 44.1 kHz -> ~1.45 samples at 16 kHz, rounded to 1.
    expect(out.length).toBe(1);
    expect(out[0]).toBe(0);
    const longer = resample(new Float32Array(441).map((_, i) => i / 441), 44100, 16000);
    expect(longer.length).toBe(160);
    // Monotonic input stays monotonic after interpolation.
    for (let i = 1; i < longer.length; i++) expect(longer[i]).toBeGreaterThanOrEqual(longer[i - 1]);
  });
});

describe('floatToPcm16', () => {
  it('packs little-endian 16-bit samples and clips', () => {
    const view = new DataView(floatToPcm16(new Float32Array([0, 0.5, -1, 1, 2, -2])));
    expect(view.getInt16(0, true)).toBe(0);
    expect(view.getInt16(2, true)).toBe(Math.round(0.5 * 0x7fff));
    expect(view.getInt16(4, true)).toBe(-32768);
    expect(view.getInt16(6, true)).toBe(32767);
    expect(view.getInt16(8, true)).toBe(32767);
    expect(view.getInt16(10, true)).toBe(-32768);
  });
});

describe('rms', () => {
  it('is 0 for an empty buffer and the amplitude for a constant one', () => {
    expect(rms(new Float32Array(0))).toBe(0);
    expect(rms(new Float32Array([0.5, -0.5, 0.5, -0.5]))).toBeCloseTo(0.5, 6);
  });
});

describe('FrameBatcher', () => {
  it('emits fixed-size 16 kHz frames and keeps the remainder', () => {
    const batcher = new FrameBatcher(48000, 160); // 10 ms frames
    // 1000 input samples at 48 kHz -> 333 output samples: two frames + 13 left.
    const frames = batcher.push(new Float32Array(1000).fill(0.1));
    expect(frames).toHaveLength(2);
    expect(frames[0].length).toBe(160);
    expect(batcher.flush()?.length).toBe(13);
    expect(batcher.flush()).toBeNull();
  });

  it('carries the remainder into the next push', () => {
    const batcher = new FrameBatcher(16000, 100);
    expect(batcher.push(new Float32Array(150))).toHaveLength(1);
    expect(batcher.push(new Float32Array(60))).toHaveLength(1);
    expect(batcher.flush()?.length).toBe(10);
  });
});
