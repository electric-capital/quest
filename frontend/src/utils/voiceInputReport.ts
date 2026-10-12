/**
 * Diagnostic report for a failed voice-input attempt.
 *
 * Microphone capture fails in browser-specific ways that the user cannot
 * see (an iPhone in an in-app browser rejects the microphone, an iOS audio
 * session change ends the capture track, a transcription upload is refused)
 * and there is no console on a phone. So every failure carries a compact
 * plain-text report -- what the browser is, which capability checks passed,
 * a timeline of what the recorder did, and the error that stopped it --
 * that the composer shows behind the error notice for the user to screenshot
 * or copy into a debugging session.
 *
 * Everything here is pure: the hook collects the facts, this module lays
 * them out. The report is meant to fit on one phone screen, so the fields
 * are terse and the timeline carries relative timestamps only.
 */

export type VoiceInputStage =
  | 'open_microphone'
  | 'create_recorder'
  | 'recording'
  | 'transcribe';

export const VOICE_INPUT_STAGE_LABELS: Record<VoiceInputStage, string> = {
  open_microphone: 'opening the microphone',
  create_recorder: 'creating the recorder',
  recording: 'recording',
  transcribe: 'transcribing the clip',
};

/** What the recorder knew about the clip when it stopped (or failed). */
export interface VoiceInputClipInfo {
  mimeType: string;
  bytes: number;
  durationMs: number;
  chunks: number;
}

export interface VoiceInputDiagnostics {
  stage: VoiceInputStage;
  /** The human notice shown to the user. */
  message: string;
  /** `describeError()` of the failure; empty when there was no exception. */
  error: string;
  /** Ordered environment facts (browser, capability checks). */
  environment: [string, string][];
  /** Relative-time log lines, oldest first ("+12ms getUserMedia ..."). */
  timeline: string[];
  clip?: VoiceInputClipInfo;
  /** When the report was built; injectable for deterministic tests. */
  at?: Date;
}

/** `Name: message` for any thrown value, with the failed constraint when known. */
export function describeError(error: unknown): string {
  if (typeof error === 'string') return error;
  if (!error || typeof error !== 'object') return String(error);
  // Duck-typed rather than `instanceof Error`: a DOMException from another
  // realm (iframes, jsdom) carries the same fields without the prototype.
  const { name, message, constraint, statusCode, errorCode } = error as {
    name?: unknown; message?: unknown; constraint?: unknown; statusCode?: unknown; errorCode?: unknown;
  };
  if (typeof name !== 'string' && typeof message !== 'string') return String(error);
  const suffix = typeof constraint === 'string' && constraint ? ` (constraint: ${constraint})` : '';
  const http = typeof statusCode === 'number'
    ? ` [HTTP ${statusCode}${typeof errorCode === 'string' && errorCode ? ` ${errorCode}` : ''}]`
    : '';
  const shownName = typeof name === 'string' && name ? name : 'Error';
  const shownMessage = typeof message === 'string' && message ? message : '(no message)';
  return `${shownName}: ${shownMessage}${suffix}${http}`;
}

/** Yes/no spelling for the capability lines. */
export function yesNo(value: boolean): string {
  return value ? 'yes' : 'no';
}

/**
 * Snapshot of the browser facts that decide whether capture can work.
 * Guarded for jsdom and old browsers; every probe degrades to "n/a".
 */
export function collectVoiceInputEnvironment(): [string, string][] {
  const env: [string, string][] = [];
  if (typeof window === 'undefined' || typeof navigator === 'undefined') {
    env.push(['browser', 'n/a (no window)']);
    return env;
  }
  env.push(['browser', navigator.userAgent || 'n/a']);
  try {
    env.push(['page', `${window.location.host}${window.location.pathname}`]);
  } catch {
    env.push(['page', 'n/a']);
  }
  const standalone = (navigator as { standalone?: boolean }).standalone === true
    || (typeof window.matchMedia === 'function' && window.matchMedia('(display-mode: standalone)').matches);
  let focused = 'n/a';
  try { focused = yesNo(document.hasFocus()); } catch { /* jsdom without focus support */ }
  env.push(['display', `standalone=${yesNo(standalone)} visibility=${document.visibilityState ?? 'n/a'} focus=${focused}`]);
  env.push(['secure context', yesNo(Boolean(window.isSecureContext))]);
  env.push(['mediaDevices', yesNo(typeof navigator.mediaDevices?.getUserMedia === 'function')]);
  env.push(['MediaRecorder', yesNo(typeof MediaRecorder !== 'undefined')]);
  env.push(['AudioContext', yesNo(typeof AudioContext !== 'undefined')]);
  return env;
}

/** `type=yes/no` pairs for MediaRecorder.isTypeSupported over the given types. */
export function describeMimeSupport(types: readonly string[]): string {
  if (typeof MediaRecorder === 'undefined' || typeof MediaRecorder.isTypeSupported !== 'function') {
    return 'n/a';
  }
  return types
    .map((type) => {
      let supported = false;
      try { supported = MediaRecorder.isTypeSupported(type); } catch { /* treat as unsupported */ }
      return `${type}=${yesNo(supported)}`;
    })
    .join(' ');
}

/** Lay the diagnostics out as the plain-text report shown in the details modal. */
export function formatVoiceInputReport(diag: VoiceInputDiagnostics): string {
  const at = diag.at ?? new Date();
  const lines: string[] = [
    'Quest voice input report',
    `when: ${at.toISOString()}`,
    `stage: ${diag.stage} (${VOICE_INPUT_STAGE_LABELS[diag.stage]})`,
    `notice: ${diag.message}`,
    `error: ${diag.error || '(none)'}`,
  ];
  for (const [key, value] of diag.environment) lines.push(`${key}: ${value}`);
  if (diag.clip) {
    const { mimeType, bytes, durationMs, chunks } = diag.clip;
    lines.push(`clip: ${mimeType || '(no type)'} ${bytes} bytes, ${(durationMs / 1000).toFixed(1)} s, ${chunks} chunk${chunks === 1 ? '' : 's'}`);
  }
  lines.push('timeline:');
  if (diag.timeline.length === 0) {
    lines.push('  (empty)');
  } else {
    for (const entry of diag.timeline) lines.push(`  ${entry}`);
  }
  return lines.join('\n');
}
