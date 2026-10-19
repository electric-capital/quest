/**
 * Live dictation: stream the microphone to the server's local speech model
 * and show the transcript while the user is still talking.
 *
 * The clip mode of voice input (hooks/useVoiceRecorder.ts) records a whole
 * utterance and uploads it afterwards. This hook is the live mode the
 * composer uses when ``GET /me`` reports ``voice_input_live``: it opens
 * ``WS /app/api/transcribe/live`` (chat/routes/transcribe_live.py), captures
 * the microphone through an AudioWorklet, resamples it to 16 kHz PCM16
 * (utils/pcm.ts) and sends ~200 ms binary frames; the server answers with
 * ``partial`` (the segment being spoken, replaced on every update) and
 * ``final`` (a finished segment, appended to ``committedText``) events. The
 * composer renders ``committedText + partialText`` into the textarea.
 *
 * State machine: idle -> connecting -> listening -> finishing -> idle.
 *
 * - ``connecting``: the microphone and the socket are opened together; the
 *   server may send ``loading`` first (``isLoadingModel``) while the weights
 *   load. Audio is only sent after ``ready`` AND the capture pipeline runs.
 * - ``listening``: frames flow; ``inputLevel`` (0..1) feeds the level
 *   meter and ``elapsedSeconds`` the timer. The hook auto-stops at
 *   ``MAX_LIVE_SECONDS`` (under the server's own session cap).
 * - ``finishing``: ``stop()`` released the microphone and sent
 *   ``{"op": "stop"}``; the server decodes the open segment and replies
 *   ``done``, then ``onFinished(committedText)`` fires. ``stop()`` during
 *   ``connecting`` is a cancel (nothing was said yet).
 * - ``cancel()`` tears everything down without a callback (composer lock,
 *   conversation switch, unmount).
 *
 * Failures go to ``onError(message, diagnostics)`` with the same report
 * format as the clip mode (utils/voiceInputReport.ts), so a phone user can
 * screenshot what went wrong. The microphone is opened the same careful way
 * (``openMicrophone``: the default device by id, with the browser's echo
 * cancellation / noise suppression / auto gain on, which also keeps the
 * server's energy-based segmenter honest).
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { endpoints } from '../api/config';
import {
  RunTrace,
  describeCaptureError,
  describeTrack,
  openMicrophone,
} from './useVoiceRecorder';
import { describeError } from '../utils/voiceInputReport';
import type { VoiceInputDiagnostics, VoiceInputStage } from '../utils/voiceInputReport';
import {
  FrameBatcher,
  TARGET_SAMPLE_RATE,
  createCaptureWorkletUrl,
  floatToPcm16,
  rms,
} from '../utils/pcm';

export type LiveDictationStatus = 'idle' | 'connecting' | 'listening' | 'finishing';

/** Auto-stop, kept under the server's MAX_SESSION_SECONDS (600). */
export const MAX_LIVE_SECONDS = 570;
/** How long ``finishing`` waits for the server's ``done``. */
export const FINISH_TIMEOUT_MS = 70_000;
/** How long ``connecting`` waits for ``ready`` (model loads can take a while). */
export const CONNECT_TIMEOUT_MS = 120_000;
/** Output samples per binary frame (200 ms at 16 kHz). */
export const FRAME_SAMPLES = TARGET_SAMPLE_RATE / 5;

// RMS that maps to a full level meter; ordinary speech sits around 0.05-0.3.
const FULL_SCALE_RMS = 0.25;
const LEVEL_STEPS = 10;
const LEVEL_UPDATE_INTERVAL_MS = 80;

/** Whether this browser + origin can run live dictation. */
export function isLiveDictationSupported(): boolean {
  if (typeof window === 'undefined' || typeof navigator === 'undefined') return false;
  if (!window.isSecureContext) return false;
  if (typeof WebSocket === 'undefined' || typeof AudioContext === 'undefined') return false;
  return typeof navigator.mediaDevices?.getUserMedia === 'function';
}

/** Join transcript pieces with single spaces, ignoring empties. */
export function joinTranscript(...parts: string[]): string {
  return parts.map((p) => p.trim()).filter(Boolean).join(' ');
}

interface ServerEvent {
  type: 'loading' | 'ready' | 'partial' | 'final' | 'done' | 'error';
  text?: string;
  code?: string;
  message?: string;
}

interface UseLiveDictationOptions {
  /** The whole dictation's committed text, once the server has sent ``done``. */
  onFinished: (text: string) => void;
  onError: (message: string, diagnostics: VoiceInputDiagnostics) => void;
  /** Force a language (server ``LANGUAGES``); undefined = auto-detect. */
  language?: string | null;
  maxSeconds?: number;
}

interface Pipeline {
  context: AudioContext;
  node: AudioWorkletNode | ScriptProcessorNode;
  source: MediaStreamAudioSourceNode;
  workletUrl: string | null;
}

export function useLiveDictation({
  onFinished,
  onError,
  language = null,
  maxSeconds = MAX_LIVE_SECONDS,
}: UseLiveDictationOptions) {
  const [status, setStatus] = useState<LiveDictationStatus>('idle');
  const [isLoadingModel, setIsLoadingModel] = useState(false);
  const [committedText, setCommittedText] = useState('');
  const [partialText, setPartialText] = useState('');
  const [inputLevel, setInputLevel] = useState(0);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);

  const statusRef = useRef<LiveDictationStatus>('idle');
  const wsRef = useRef<WebSocket | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const pipelineRef = useRef<Pipeline | null>(null);
  const batcherRef = useRef<FrameBatcher | null>(null);
  const traceRef = useRef<RunTrace | null>(null);
  const committedRef = useRef('');
  const timerRef = useRef<number | null>(null);
  const maxTimerRef = useRef<number | null>(null);
  const finishTimerRef = useRef<number | null>(null);
  const startedAtRef = useRef(0);
  const framesSentRef = useRef(0);
  // Bumped on every start/cancel so a stale async step can tell it lost.
  const runIdRef = useRef(0);
  const onFinishedRef = useRef(onFinished);
  const onErrorRef = useRef(onError);
  useEffect(() => { onFinishedRef.current = onFinished; }, [onFinished]);
  useEffect(() => { onErrorRef.current = onError; }, [onError]);

  const setStatusBoth = useCallback((next: LiveDictationStatus) => {
    statusRef.current = next;
    setStatus(next);
  }, []);

  const clearTimers = useCallback(() => {
    for (const ref of [timerRef, maxTimerRef, finishTimerRef]) {
      if (ref.current !== null) {
        window.clearTimeout(ref.current);
        window.clearInterval(ref.current);
        ref.current = null;
      }
    }
  }, []);

  /** Stop capturing: disconnect the audio graph and release the microphone. */
  const releaseCapture = useCallback(() => {
    const pipeline = pipelineRef.current;
    pipelineRef.current = null;
    if (pipeline) {
      try { pipeline.node.disconnect(); } catch { /* ignore */ }
      try { pipeline.source.disconnect(); } catch { /* ignore */ }
      if ('port' in pipeline.node) pipeline.node.port.onmessage = null;
      else (pipeline.node as ScriptProcessorNode).onaudioprocess = null;
      void pipeline.context.close().catch(() => undefined);
      if (pipeline.workletUrl) URL.revokeObjectURL(pipeline.workletUrl);
    }
    const stream = streamRef.current;
    streamRef.current = null;
    if (stream) {
      for (const track of stream.getTracks()) {
        try { track.stop(); } catch { /* ignore */ }
      }
    }
    setInputLevel(0);
  }, []);

  /** Tear everything down and return to idle (no callbacks). */
  const teardown = useCallback(() => {
    runIdRef.current += 1;
    clearTimers();
    releaseCapture();
    const ws = wsRef.current;
    wsRef.current = null;
    if (ws) {
      ws.onopen = null;
      ws.onmessage = null;
      ws.onerror = null;
      ws.onclose = null;
      try { ws.close(); } catch { /* ignore */ }
    }
    batcherRef.current = null;
    setIsLoadingModel(false);
    setStatusBoth('idle');
    setElapsedSeconds(0);
  }, [clearTimers, releaseCapture, setStatusBoth]);

  const fail = useCallback((stage: VoiceInputStage, message: string, error?: unknown) => {
    const trace = traceRef.current ?? new RunTrace();
    trace.log(`failed: ${message}`);
    const diagnostics = trace.diagnostics(stage, message, error, {
      mimeType: `audio/pcm;rate=${TARGET_SAMPLE_RATE}`,
      bytes: framesSentRef.current * FRAME_SAMPLES * 2,
      durationMs: startedAtRef.current ? Date.now() - startedAtRef.current : 0,
      chunks: framesSentRef.current,
    });
    teardown();
    setCommittedText('');
    setPartialText('');
    committedRef.current = '';
    onErrorRef.current(message, diagnostics);
  }, [teardown]);

  const cancel = useCallback(() => {
    if (statusRef.current === 'idle') return;
    traceRef.current?.log('cancelled');
    teardown();
    setCommittedText('');
    setPartialText('');
    committedRef.current = '';
  }, [teardown]);

  const stop = useCallback(() => {
    const current = statusRef.current;
    if (current === 'idle' || current === 'finishing') return;
    if (current === 'connecting') {
      // Nothing has been said yet: a stop here is a cancel.
      cancel();
      return;
    }
    const trace = traceRef.current;
    const ws = wsRef.current;
    // Flush the sub-frame remainder, then release the microphone.
    const tail = batcherRef.current?.flush();
    if (tail && tail.length > 0 && ws && ws.readyState === WebSocket.OPEN) {
      ws.send(floatToPcm16(tail));
      framesSentRef.current += 1;
    }
    releaseCapture();
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    if (maxTimerRef.current !== null) {
      window.clearTimeout(maxTimerRef.current);
      maxTimerRef.current = null;
    }
    trace?.log(`stop after ${framesSentRef.current} frames`);
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      fail('live_dictation', 'Live dictation lost its connection to the server.');
      return;
    }
    setStatusBoth('finishing');
    try {
      ws.send(JSON.stringify({ op: 'stop' }));
    } catch (error) {
      fail('live_dictation', 'Live dictation lost its connection to the server.', error);
      return;
    }
    finishTimerRef.current = window.setTimeout(() => {
      fail('live_dictation', 'The server took too long to finish the transcript.');
    }, FINISH_TIMEOUT_MS);
  }, [cancel, fail, releaseCapture, setStatusBoth]);

  const start = useCallback(async () => {
    if (statusRef.current !== 'idle') return;
    const trace = new RunTrace();
    traceRef.current = trace;
    framesSentRef.current = 0;
    startedAtRef.current = 0;
    committedRef.current = '';
    setCommittedText('');
    setPartialText('');
    if (!isLiveDictationSupported()) {
      fail('open_microphone', 'Live dictation is not available in this browser.');
      return;
    }
    const runId = runIdRef.current + 1;
    runIdRef.current = runId;
    const stale = () => runIdRef.current !== runId;
    setStatusBoth('connecting');

    // Open the socket and the microphone together; the socket's ready
    // handshake (possibly a model load) and the permission prompt overlap.
    const connect = new Promise<WebSocket>((resolve, reject) => {
      let ws: WebSocket;
      try {
        ws = new WebSocket(endpoints.transcribeLive(language));
      } catch (error) {
        reject(error);
        return;
      }
      ws.binaryType = 'arraybuffer';
      wsRef.current = ws;
      const timeout = window.setTimeout(() => {
        reject(new Error(`no ready event within ${CONNECT_TIMEOUT_MS}ms`));
      }, CONNECT_TIMEOUT_MS);
      ws.onopen = () => trace.log('socket open');
      ws.onerror = () => trace.log('socket error event');
      ws.onclose = (event) => {
        trace.log(`socket closed before ready: code=${event.code} reason=${event.reason || '(none)'}`);
        window.clearTimeout(timeout);
        reject(new Error(`connection closed (code ${event.code})`));
      };
      ws.onmessage = (message) => {
        let event: ServerEvent;
        try {
          event = JSON.parse(String(message.data)) as ServerEvent;
        } catch {
          return;
        }
        if (event.type === 'loading') {
          trace.log('server is loading the speech model');
          setIsLoadingModel(true);
        } else if (event.type === 'ready') {
          trace.log('server ready');
          window.clearTimeout(timeout);
          setIsLoadingModel(false);
          resolve(ws);
        } else if (event.type === 'error') {
          trace.log(`server refused: ${event.code}: ${event.message}`);
          window.clearTimeout(timeout);
          const error = new Error(event.message || 'Live dictation is unavailable.');
          (error as Error & { errorCode?: string }).errorCode = event.code;
          reject(error);
        }
      };
    });

    // The stream is registered the moment it opens so a cancel or a socket
    // failure while the other half of the handshake is still pending
    // releases it through teardown(); one that opens after the run was
    // already torn down is stopped on the spot.
    const microphone = openMicrophone(trace, {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    }).then((opened) => {
      if (stale()) {
        for (const track of opened.getTracks()) track.stop();
      } else {
        streamRef.current = opened;
      }
      return opened;
    });
    let stream: MediaStream;
    let ws: WebSocket;
    try {
      [stream, ws] = await Promise.all([microphone, connect]);
    } catch (error) {
      if (stale()) return;
      const code = (error as { errorCode?: string } | null)?.errorCode;
      const message = code === 'voice_input_disabled'
        ? 'Voice input has been turned off by an administrator.'
        : code === 'live_transcription_unavailable'
          ? `Live dictation is not available right now: ${(error as Error).message}`
          : code === 'busy'
            ? (error as Error).message
            : code !== undefined || (error instanceof Error && /connection|ready/.test(error.message))
              ? `Could not connect to live dictation: ${(error as Error).message}`
              : describeCaptureError(error);
      const stage: VoiceInputStage = code !== undefined || !(error && typeof error === 'object' && 'name' in error)
        ? 'connect_live' : 'open_microphone';
      // The socket may have opened while the microphone failed; close it.
      // (The microphone, open or opening, is released by fail() -> teardown
      // or by the stale-run guard above.)
      void connect.then((s) => s.close(), () => undefined);
      fail(stage, message, error);
      return;
    }
    if (stale()) {
      for (const track of stream.getTracks()) track.stop();
      ws.close();
      return;
    }
    const tracks = stream.getAudioTracks();
    trace.log(`stream: ${tracks.length} audio track${tracks.length === 1 ? '' : 's'}${tracks[0] ? ` ${describeTrack(tracks[0])}` : ''}`);

    // Build the capture pipeline: AudioWorklet where available, the
    // deprecated ScriptProcessorNode otherwise.
    let pipeline: Pipeline;
    try {
      const context = new AudioContext();
      if (context.state !== 'running') {
        await context.resume().catch(() => undefined);
      }
      if (stale()) {
        void context.close().catch(() => undefined);
        for (const track of stream.getTracks()) track.stop();
        ws.close();
        return;
      }
      const source = context.createMediaStreamSource(stream);
      const batcher = new FrameBatcher(context.sampleRate, FRAME_SAMPLES);
      batcherRef.current = batcher;
      let lastLevelAt = 0;
      let lastLevel = 0;
      const onSamples = (samples: Float32Array) => {
        if (statusRef.current !== 'listening' || runIdRef.current !== runId) return;
        const socket = wsRef.current;
        if (!socket || socket.readyState !== WebSocket.OPEN) return;
        for (const frame of batcher.push(samples)) {
          socket.send(floatToPcm16(frame));
          framesSentRef.current += 1;
          const now = Date.now();
          if (now - lastLevelAt >= LEVEL_UPDATE_INTERVAL_MS) {
            lastLevelAt = now;
            const level = Math.round(Math.min(1, rms(frame) / FULL_SCALE_RMS) * LEVEL_STEPS) / LEVEL_STEPS;
            if (level !== lastLevel) {
              lastLevel = level;
              setInputLevel(level);
            }
          }
        }
      };
      let node: AudioWorkletNode | ScriptProcessorNode;
      let workletUrl: string | null = null;
      if (context.audioWorklet && typeof AudioWorkletNode !== 'undefined') {
        workletUrl = createCaptureWorkletUrl();
        await context.audioWorklet.addModule(workletUrl);
        const worklet = new AudioWorkletNode(context, 'quest-pcm-capture', {
          numberOfInputs: 1,
          numberOfOutputs: 0,
          channelCount: 1,
        });
        worklet.port.onmessage = (event: MessageEvent<Float32Array>) => onSamples(event.data);
        source.connect(worklet);
        node = worklet;
        trace.log(`AudioWorklet capture at ${context.sampleRate}Hz`);
      } else {
        const processor = context.createScriptProcessor(4096, 1, 1);
        processor.onaudioprocess = (event) => onSamples(event.inputBuffer.getChannelData(0));
        source.connect(processor);
        // A ScriptProcessorNode only runs while connected to the destination.
        processor.connect(context.destination);
        node = processor;
        trace.log(`ScriptProcessor capture at ${context.sampleRate}Hz`);
      }
      pipeline = { context, node, source, workletUrl };
    } catch (error) {
      if (stale()) return;
      trace.log(`capture pipeline failed: ${describeError(error)}`);
      fail('create_recorder', 'Could not start live dictation in this browser.', error);
      return;
    }
    if (stale()) {
      releaseCapture();
      return;
    }
    pipelineRef.current = pipeline;

    // Live: route server events to the transcript from here on.
    ws.onmessage = (message) => {
      let event: ServerEvent;
      try {
        event = JSON.parse(String(message.data)) as ServerEvent;
      } catch {
        return;
      }
      if (event.type === 'partial') {
        setPartialText(event.text ?? '');
      } else if (event.type === 'final') {
        committedRef.current = joinTranscript(committedRef.current, event.text ?? '');
        setCommittedText(committedRef.current);
        setPartialText('');
      } else if (event.type === 'done') {
        trace.log(`done: ${committedRef.current.length} chars committed`);
        const text = committedRef.current;
        teardown();
        setPartialText('');
        onFinishedRef.current(text);
      } else if (event.type === 'error') {
        fail('live_dictation', event.message || 'Live dictation failed on the server.', new Error(event.code));
      }
    };
    ws.onclose = (event) => {
      if (runIdRef.current !== runId) return;
      trace.log(`socket closed: code=${event.code} reason=${event.reason || '(none)'}`);
      if (statusRef.current === 'listening' || statusRef.current === 'finishing') {
        fail('live_dictation', 'Live dictation lost its connection to the server.', new Error(`close code ${event.code}`));
      }
    };
    for (const track of tracks) {
      track.addEventListener('ended', () => {
        if (runIdRef.current !== runId || statusRef.current !== 'listening') return;
        trace.log(`capture track ended (${describeTrack(track)})`);
        stop();
      });
    }

    startedAtRef.current = Date.now();
    setElapsedSeconds(0);
    setStatusBoth('listening');
    trace.log('listening');
    timerRef.current = window.setInterval(() => {
      setElapsedSeconds(Math.floor((Date.now() - startedAtRef.current) / 1000));
    }, 500);
    maxTimerRef.current = window.setTimeout(() => stop(), maxSeconds * 1000);
  }, [language, maxSeconds, fail, releaseCapture, setStatusBoth, stop, teardown]);

  // Never leave the microphone or the socket open after unmount.
  useEffect(() => () => {
    teardown();
  }, [teardown]);

  return {
    status,
    isLoadingModel,
    committedText,
    partialText,
    inputLevel,
    elapsedSeconds,
    start,
    stop,
    cancel,
  };
}
