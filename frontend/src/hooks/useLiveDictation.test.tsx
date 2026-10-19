// useLiveDictation under fake media, Web Audio and WebSocket APIs: the
// connect handshake, PCM frames leaving as 16 kHz int16, partial / final
// events building the transcript, the stop -> done hand-off, and the
// failure paths (server refusal, lost connection, cancel while connecting).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { FRAME_SAMPLES, joinTranscript, useLiveDictation } from './useLiveDictation';
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

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  readyState = FakeWebSocket.OPEN;
  binaryType = 'blob';
  url: string;
  sent: (string | ArrayBuffer)[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((e: { code: number; reason: string }) => void) | null = null;
  close = vi.fn(() => { this.readyState = FakeWebSocket.CLOSED; });
  send = vi.fn((data: string | ArrayBuffer) => { this.sent.push(data); });
  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }
  /** Deliver a server event. */
  serverSays(event: Record<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify(event) });
  }
}

class FakeWorkletNode {
  static instances: FakeWorkletNode[] = [];
  port: { onmessage: ((e: { data: Float32Array }) => void) | null } = { onmessage: null };
  disconnect = vi.fn();
  name: string;
  constructor(_context: unknown, name: string) {
    this.name = name;
    FakeWorkletNode.instances.push(this);
  }
}

class FakeAudioContext {
  static instances: FakeAudioContext[] = [];
  sampleRate = 48000;
  state = 'running';
  audioWorklet = { addModule: vi.fn(() => Promise.resolve()) };
  close = vi.fn(() => Promise.resolve());
  resume = vi.fn(() => Promise.resolve());
  createMediaStreamSource() { return { connect: vi.fn(), disconnect: vi.fn() }; }
  constructor() { FakeAudioContext.instances.push(this); }
}

function renderDictation() {
  const onFinished = vi.fn<(text: string) => void>();
  const onError = vi.fn<(m: string, d: VoiceInputDiagnostics) => void>();
  const { result } = renderHook(() => useLiveDictation({ onFinished, onError }));
  return { onFinished, onError, result };
}

async function flush() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

async function kickStart(result: { current: { start: () => Promise<void> } }) {
  await act(async () => {
    void result.current.start();
    await vi.advanceTimersByTimeAsync(0);
  });
}

/** Start and complete the handshake so the hook is listening. */
async function startListening(result: ReturnType<typeof renderDictation>['result']) {
  await kickStart(result);
  const ws = FakeWebSocket.instances[0];
  await act(async () => {
    ws.serverSays({ type: 'ready' });
    await vi.advanceTimersByTimeAsync(0);
  });
  await flush();
  return ws;
}

describe('joinTranscript', () => {
  it('joins non-empty trimmed pieces with single spaces', () => {
    expect(joinTranscript('', ' a ', '', 'b')).toBe('a b');
    expect(joinTranscript('', '  ')).toBe('');
  });
});

describe('useLiveDictation', () => {
  let getUserMedia: ReturnType<typeof vi.fn>;
  let track: FakeTrack;

  beforeEach(() => {
    vi.useFakeTimers();
    FakeWebSocket.instances = [];
    FakeWorkletNode.instances = [];
    FakeAudioContext.instances = [];
    track = new FakeTrack();
    getUserMedia = vi.fn(() => Promise.resolve(fakeStream(track)));
    Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true });
    Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia }, configurable: true });
    vi.stubGlobal('WebSocket', FakeWebSocket);
    vi.stubGlobal('AudioContext', FakeAudioContext);
    vi.stubGlobal('AudioWorkletNode', FakeWorkletNode);
    vi.stubGlobal('URL', Object.assign(URL, {
      createObjectURL: vi.fn(() => 'blob:worklet'),
      revokeObjectURL: vi.fn(),
    }));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('streams 16 kHz PCM16 frames once the server is ready and builds the transcript', async () => {
    const { result, onFinished, onError } = renderDictation();
    await kickStart(result);
    expect(result.current.status).toBe('connecting');
    const ws = FakeWebSocket.instances[0];
    expect(ws.url).toMatch(/\/app\/api\/transcribe\/live$/);
    expect(ws.binaryType).toBe('arraybuffer');
    expect(getUserMedia).toHaveBeenCalledWith({
      audio: expect.objectContaining({ channelCount: 1, deviceId: { exact: 'default' } }),
    });

    await act(async () => {
      ws.serverSays({ type: 'loading' });
    });
    expect(result.current.isLoadingModel).toBe(true);
    await act(async () => {
      ws.serverSays({ type: 'ready' });
      await vi.advanceTimersByTimeAsync(0);
    });
    await flush();
    expect(result.current.status).toBe('listening');
    expect(result.current.isLoadingModel).toBe(false);
    expect(FakeAudioContext.instances[0].audioWorklet.addModule).toHaveBeenCalledWith('blob:worklet');

    // 48 kHz capture: 3 x FRAME_SAMPLES input samples make one 16 kHz frame.
    const worklet = FakeWorkletNode.instances[0];
    await act(async () => {
      worklet.port.onmessage?.({ data: new Float32Array(FRAME_SAMPLES * 3).fill(0.25) });
    });
    const binary = ws.sent.filter((d): d is ArrayBuffer => d instanceof ArrayBuffer);
    expect(binary).toHaveLength(1);
    expect(binary[0].byteLength).toBe(FRAME_SAMPLES * 2);
    expect(new DataView(binary[0]).getInt16(0, true)).toBe(Math.round(0.25 * 0x7fff));
    expect(result.current.inputLevel).toBeGreaterThan(0);

    await act(async () => {
      ws.serverSays({ type: 'partial', text: 'hello th' });
    });
    expect(result.current.partialText).toBe('hello th');
    await act(async () => {
      ws.serverSays({ type: 'final', text: 'hello there.' });
      ws.serverSays({ type: 'partial', text: 'how' });
    });
    expect(result.current.committedText).toBe('hello there.');
    expect(result.current.partialText).toBe('how');

    await act(async () => {
      result.current.stop();
    });
    expect(result.current.status).toBe('finishing');
    expect(track.stop).toHaveBeenCalled();
    expect(ws.sent.at(-1)).toBe(JSON.stringify({ op: 'stop' }));

    await act(async () => {
      ws.serverSays({ type: 'final', text: 'how are you?' });
      ws.serverSays({ type: 'done' });
    });
    expect(result.current.status).toBe('idle');
    expect(onFinished).toHaveBeenCalledWith('hello there. how are you?');
    expect(onError).not.toHaveBeenCalled();
    expect(ws.close).toHaveBeenCalled();
  });

  it('reports a server refusal with the connect stage and releases the microphone', async () => {
    const { result, onError, onFinished } = renderDictation();
    await kickStart(result);
    const ws = FakeWebSocket.instances[0];
    await act(async () => {
      ws.serverSays({ type: 'error', code: 'live_transcription_unavailable', message: 'no model' });
      await vi.advanceTimersByTimeAsync(0);
    });
    await flush();
    expect(result.current.status).toBe('idle');
    expect(onFinished).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledTimes(1);
    const [message, diag] = onError.mock.calls[0];
    expect(message).toMatch(/Live dictation is not available right now: no model/);
    expect(diag.stage).toBe('connect_live');
    expect(diag.timeline.some((l) => l.includes('server refused: live_transcription_unavailable'))).toBe(true);
    expect(track.stop).toHaveBeenCalled();
  });

  it('reports a lost connection while listening and keeps nothing running', async () => {
    const { result, onError } = renderDictation();
    const ws = await startListening(result);
    expect(result.current.status).toBe('listening');
    await act(async () => {
      ws.onclose?.({ code: 1006, reason: '' });
    });
    expect(result.current.status).toBe('idle');
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][0]).toMatch(/lost its connection/);
    expect(onError.mock.calls[0][1].stage).toBe('live_dictation');
    expect(track.stop).toHaveBeenCalled();
    expect(FakeAudioContext.instances[0].close).toHaveBeenCalled();
  });

  it('treats stop while connecting as a cancel', async () => {
    const { result, onError, onFinished } = renderDictation();
    await kickStart(result);
    await act(async () => {
      result.current.stop();
    });
    expect(result.current.status).toBe('idle');
    expect(FakeWebSocket.instances[0].close).toHaveBeenCalled();
    // The microphone that opens later is released by the stale-run guard.
    await flush();
    expect(track.stop).toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
    expect(onFinished).not.toHaveBeenCalled();
  });

  it('stops by itself when the capture track ends', async () => {
    const { result } = renderDictation();
    const ws = await startListening(result);
    await act(async () => {
      track.dispatchEvent(new Event('ended'));
    });
    expect(result.current.status).toBe('finishing');
    expect(ws.sent.at(-1)).toBe(JSON.stringify({ op: 'stop' }));
  });
});
