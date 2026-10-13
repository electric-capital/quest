// useVoiceRecorder under fake media APIs: the failure paths hand the caller
// diagnostics with the run's timeline, a never-settling AudioContext.resume()
// (iOS Safari) cannot leave the state stuck in "starting", and a capture
// track that ends under the recorder stops the run cleanly.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import {
  AUDIO_CONTEXT_RESUME_TIMEOUT_MS,
  LIVE_SIGNAL_TIMEOUT_MS,
  useVoiceRecorder,
} from './useVoiceRecorder';
import type { VoiceRecording } from './useVoiceRecorder';
import type { VoiceInputDiagnostics } from '../utils/voiceInputReport';

class FakeTrack extends EventTarget {
  kind = 'audio';
  label = 'Fake Microphone';
  readyState: 'live' | 'ended' = 'live';
  muted = false;
  stop = vi.fn(() => { this.readyState = 'ended'; });
  getSettings() { return { sampleRate: 48000, channelCount: 1 }; }
}

function fakeStream(track: FakeTrack) {
  return {
    getTracks: () => [track],
    getAudioTracks: () => [track],
  } as unknown as MediaStream;
}

/** Minimal MediaRecorder: start/stop drive the state and fire onstop. */
class FakeMediaRecorder {
  static instances: FakeMediaRecorder[] = [];
  static isTypeSupported(type: string) { return type === 'audio/mp4'; }
  state: 'inactive' | 'recording' = 'inactive';
  mimeType: string;
  ondataavailable: ((e: { data: Blob }) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  onstop: (() => void) | null = null;
  start = vi.fn(() => { this.state = 'recording'; });
  stop = vi.fn(() => {
    this.state = 'inactive';
    this.onstop?.();
  });
  constructor(_stream: MediaStream, options?: { mimeType?: string }) {
    this.mimeType = options?.mimeType ?? '';
    FakeMediaRecorder.instances.push(this);
  }
  /** Deliver a chunk as the browser would on a timeslice. */
  emit(bytes: number) {
    this.ondataavailable?.({ data: new Blob([new Uint8Array(bytes)], { type: this.mimeType }) });
  }
}

/** AudioContext whose resume() never settles, as iOS Safari can do. */
class HangingAudioContext {
  state = 'suspended';
  resume = vi.fn(() => new Promise<void>(() => undefined));
  close = vi.fn(() => Promise.resolve());
  createAnalyser() {
    return { fftSize: 1024, getFloatTimeDomainData: (buf: Float32Array) => buf.fill(0) };
  }
  createMediaStreamSource() { return { connect: () => undefined }; }
}

function domError(name: string, message = name) {
  return new DOMException(message, name);
}

interface Harness {
  onComplete: ReturnType<typeof vi.fn<(r: VoiceRecording) => void>>;
  onError: ReturnType<typeof vi.fn<(m: string, d: VoiceInputDiagnostics) => void>>;
}

function renderRecorder(): Harness & { result: ReturnType<typeof renderHook<ReturnType<typeof useVoiceRecorder>, void>>['result'] } {
  const onComplete = vi.fn<(r: VoiceRecording) => void>();
  const onError = vi.fn<(m: string, d: VoiceInputDiagnostics) => void>();
  const { result } = renderHook(() => useVoiceRecorder({
    onRecordingComplete: onComplete,
    onError,
  }));
  return { onComplete, onError, result };
}

/** Kick off start() without awaiting it (it may legitimately never settle). */
async function kickStart(result: { current: { start: () => Promise<void> } }) {
  await act(async () => {
    void result.current.start();
    await vi.advanceTimersByTimeAsync(0);
  });
}

describe('useVoiceRecorder', () => {
  let getUserMedia: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.useFakeTimers();
    FakeMediaRecorder.instances = [];
    getUserMedia = vi.fn();
    Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true });
    Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia }, configurable: true });
    vi.stubGlobal('MediaRecorder', FakeMediaRecorder);
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => window.setTimeout(() => cb(Date.now()), 16));
    vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id));
    // No Web Audio by default: the live gate is skipped and the recorder is
    // trusted immediately. Individual tests install an AudioContext.
    vi.stubGlobal('AudioContext', undefined);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('reports a refused microphone with diagnostics carrying the attempt log', async () => {
    getUserMedia.mockRejectedValueOnce(domError('NotAllowedError', 'The request is not allowed'));
    const { result, onError, onComplete } = renderRecorder();

    await kickStart(result);

    expect(result.current.status).toBe('idle');
    expect(onComplete).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledTimes(1);
    const [message, diag] = onError.mock.calls[0];
    expect(message).toMatch(/Microphone access was denied/);
    expect(diag.stage).toBe('open_microphone');
    expect(diag.error).toBe('NotAllowedError: The request is not allowed');
    expect(diag.timeline.some((l) => l.includes('getUserMedia by id rejected: NotAllowedError'))).toBe(true);
    expect(diag.environment).toEqual(expect.arrayContaining([
      ['secure context', 'yes'],
      ['MediaRecorder', 'yes'],
      ['mime support', expect.stringContaining('audio/mp4=yes')],
    ]));
    // The by-id refusal is a permission answer: no bare retry.
    expect(getUserMedia).toHaveBeenCalledTimes(1);
  });

  it('falls back to a bare request when the default id is unknown and logs both attempts', async () => {
    const track = new FakeTrack();
    getUserMedia
      .mockRejectedValueOnce(Object.assign(domError('OverconstrainedError', 'Invalid constraint'), { constraint: 'deviceId' }))
      .mockResolvedValueOnce(fakeStream(track));
    const { result, onComplete } = renderRecorder();

    await kickStart(result);
    expect(result.current.status).toBe('recording');
    const recorder = FakeMediaRecorder.instances[0];
    expect(recorder.mimeType).toBe('audio/mp4');
    expect(recorder.start).toHaveBeenCalledWith(1000);

    recorder.emit(1234);
    act(() => { result.current.stop(); });

    expect(result.current.status).toBe('idle');
    expect(track.stop).toHaveBeenCalled();
    expect(onComplete).toHaveBeenCalledTimes(1);
    const recording = onComplete.mock.calls[0][0];
    expect(recording.blob.size).toBe(1234);
    expect(recording.filename).toBe('voice-input.m4a');
    const log = recording.trace.timeline.join('\n');
    expect(log).toContain('getUserMedia by id rejected: OverconstrainedError: Invalid constraint (constraint: deviceId)');
    expect(log).toContain('getUserMedia bare {audio: true}');
    expect(log).toContain('recorder stopped: 1234 bytes in 1 chunks');
  });

  it("falls back when WebKit's OverconstrainedError is not a DOMException", async () => {
    // Safari rejects the by-id request with its own OverconstrainedError
    // class: same name and message, no DOMException prototype.
    class WebKitOverconstrainedError {
      name = 'OverconstrainedError';
      message = 'Invalid constraint';
      constraint = '';
    }
    const track = new FakeTrack();
    getUserMedia
      .mockRejectedValueOnce(new WebKitOverconstrainedError())
      .mockResolvedValueOnce(fakeStream(track));
    const { result, onError, onComplete } = renderRecorder();

    await kickStart(result);

    expect(onError).not.toHaveBeenCalled();
    expect(getUserMedia).toHaveBeenCalledTimes(2);
    expect(getUserMedia).toHaveBeenLastCalledWith({ audio: true });
    expect(result.current.status).toBe('recording');

    FakeMediaRecorder.instances[0].emit(64);
    act(() => { result.current.stop(); });
    expect(onComplete).toHaveBeenCalledTimes(1);
    expect(onComplete.mock.calls[0][0].trace.timeline.join('\n')).toContain('getUserMedia bare {audio: true}');
  });

  it('spells the denied-microphone message for a non-DOMException NotAllowedError', async () => {
    getUserMedia.mockRejectedValueOnce({ name: 'NotAllowedError', message: 'not allowed' });
    const { result, onError } = renderRecorder();

    await kickStart(result);

    expect(getUserMedia).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][0]).toMatch(/Microphone access was denied/);
  });

  it('leaves "starting" on the give-up timer when AudioContext.resume() never settles', async () => {
    vi.stubGlobal('AudioContext', HangingAudioContext);
    getUserMedia.mockResolvedValueOnce(fakeStream(new FakeTrack()));
    const { result, onComplete } = renderRecorder();

    await kickStart(result);
    expect(result.current.status).toBe('starting');

    await act(async () => { await vi.advanceTimersByTimeAsync(LIVE_SIGNAL_TIMEOUT_MS); });
    expect(result.current.status).toBe('recording');

    FakeMediaRecorder.instances[0].emit(10);
    act(() => { result.current.stop(); });
    const log = onComplete.mock.calls[0][0].trace.timeline.join('\n');
    expect(log).toContain(`resume() still pending after ${AUDIO_CONTEXT_RESUME_TIMEOUT_MS}ms`);
    expect(log).toMatch(/recording \(no (level monitor|live signal within \d+ms)\)/);
  });

  it('stops cleanly when the capture track ends and names the cause for an empty clip', async () => {
    const track = new FakeTrack();
    getUserMedia.mockResolvedValueOnce(fakeStream(track));
    const { result, onError, onComplete } = renderRecorder();

    await kickStart(result);
    expect(result.current.status).toBe('recording');

    act(() => { track.dispatchEvent(new Event('ended')); });

    expect(FakeMediaRecorder.instances[0].stop).toHaveBeenCalled();
    expect(result.current.status).toBe('idle');
    expect(onComplete).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledTimes(1);
    const [message, diag] = onError.mock.calls[0];
    expect(message).toBe('Nothing was recorded: the microphone stopped delivering audio.');
    expect(diag.stage).toBe('recording');
    expect(diag.error).toContain('capture track ended');
    expect(diag.clip).toEqual(expect.objectContaining({ bytes: 0, chunks: 0, mimeType: 'audio/mp4' }));
  });

  it('reports a MediaRecorder error once with the clip so far', async () => {
    getUserMedia.mockResolvedValueOnce(fakeStream(new FakeTrack()));
    const { result, onError, onComplete } = renderRecorder();

    await kickStart(result);
    const recorder = FakeMediaRecorder.instances[0];
    recorder.emit(500);
    act(() => {
      recorder.onerror?.(Object.assign(new Event('error'), { error: domError('UnknownError', 'encoder died') }));
    });

    expect(result.current.status).toBe('idle');
    expect(onComplete).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledTimes(1);
    const [message, diag] = onError.mock.calls[0];
    expect(message).toBe('Recording failed.');
    expect(diag.error).toBe('UnknownError: encoder died');
    expect(diag.clip?.bytes).toBe(500);
  });
});
