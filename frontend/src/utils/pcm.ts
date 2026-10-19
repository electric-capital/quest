/**
 * PCM helpers for live dictation (hooks/useLiveDictation.ts).
 *
 * The microphone arrives from Web Audio as float32 frames at the
 * AudioContext's rate (44.1 or 48 kHz); the server's speech model wants
 * 16 kHz mono 16-bit little-endian PCM. Everything here is pure so it can
 * be unit-tested without a browser audio stack.
 */

export const TARGET_SAMPLE_RATE = 16000;

/**
 * Resample mono float samples from `fromRate` to `toRate`.
 *
 * An integer downsampling ratio (48 kHz -> 16 kHz is 3:1) averages each
 * group of input samples, which is a cheap low-pass before decimation and
 * keeps speech clean. Any other ratio falls back to linear interpolation,
 * which is good enough for a recognizer at these rates.
 */
export function resample(input: Float32Array, fromRate: number, toRate: number): Float32Array {
  if (fromRate === toRate || input.length === 0) return input;
  if (fromRate > toRate && fromRate % toRate === 0) {
    const factor = fromRate / toRate;
    const outLength = Math.floor(input.length / factor);
    const out = new Float32Array(outLength);
    for (let i = 0; i < outLength; i++) {
      let sum = 0;
      const base = i * factor;
      for (let k = 0; k < factor; k++) sum += input[base + k];
      out[i] = sum / factor;
    }
    return out;
  }
  const ratio = toRate / fromRate;
  const outLength = Math.max(0, Math.round(input.length * ratio));
  const out = new Float32Array(outLength);
  for (let i = 0; i < outLength; i++) {
    const x = i / ratio;
    const x0 = Math.floor(x);
    const x1 = Math.min(x0 + 1, input.length - 1);
    const t = x - x0;
    out[i] = input[x0] * (1 - t) + input[x1] * t;
  }
  return out;
}

/** Float samples in -1..1 -> 16-bit little-endian PCM bytes. */
export function floatToPcm16(input: Float32Array): ArrayBuffer {
  const buffer = new ArrayBuffer(input.length * 2);
  const view = new DataView(buffer);
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i]));
    view.setInt16(i * 2, Math.round(s < 0 ? s * 0x8000 : s * 0x7fff), true);
  }
  return buffer;
}

/** Root-mean-square of a sample buffer (0 for an empty one). */
export function rms(samples: Float32Array): number {
  if (samples.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
  return Math.sqrt(sum / samples.length);
}

/**
 * Accumulates resampled audio and hands out fixed-size frames.
 *
 * `push()` returns every complete frame of `frameSamples` output samples
 * that the new input completed; the remainder waits for the next push.
 * `flush()` returns whatever is left (possibly short) at the end.
 */
export class FrameBatcher {
  private pending: Float32Array = new Float32Array(0);
  private readonly fromRate: number;
  private readonly frameSamples: number;
  private readonly toRate: number;

  constructor(fromRate: number, frameSamples: number, toRate: number = TARGET_SAMPLE_RATE) {
    this.fromRate = fromRate;
    this.frameSamples = frameSamples;
    this.toRate = toRate;
  }

  push(input: Float32Array): Float32Array[] {
    const resampled = resample(input, this.fromRate, this.toRate);
    const joined = new Float32Array(this.pending.length + resampled.length);
    joined.set(this.pending, 0);
    joined.set(resampled, this.pending.length);
    const frames: Float32Array[] = [];
    let offset = 0;
    while (joined.length - offset >= this.frameSamples) {
      frames.push(joined.slice(offset, offset + this.frameSamples));
      offset += this.frameSamples;
    }
    this.pending = joined.slice(offset);
    return frames;
  }

  flush(): Float32Array | null {
    if (this.pending.length === 0) return null;
    const out = this.pending;
    this.pending = new Float32Array(0);
    return out;
  }
}

/**
 * Source of the AudioWorklet processor that forwards the microphone's
 * float32 frames to the main thread. Loaded from a blob URL so the worklet
 * needs no separate build artifact (`createCaptureWorkletUrl`). The
 * processor is mono: it takes the first input channel as delivered (the
 * capture track is requested with `channelCount: 1`).
 */
export const CAPTURE_WORKLET_SOURCE = `
class QuestPcmCapture extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length > 0) {
      const copy = new Float32Array(channel.length);
      copy.set(channel);
      this.port.postMessage(copy, [copy.buffer]);
    }
    return true;
  }
}
registerProcessor('quest-pcm-capture', QuestPcmCapture);
`;

export function createCaptureWorkletUrl(): string {
  return URL.createObjectURL(new Blob([CAPTURE_WORKLET_SOURCE], { type: 'application/javascript' }));
}
