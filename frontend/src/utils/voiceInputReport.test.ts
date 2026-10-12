// voiceInputReport: error spelling and the plain-text layout of the report
// the composer shows behind a failed voice-input notice.
import { describe, expect, it } from 'vitest';
import { describeError, formatVoiceInputReport } from './voiceInputReport';

describe('describeError', () => {
  it('spells a DOMException as name: message', () => {
    const error = new DOMException('Permission denied', 'NotAllowedError');
    expect(describeError(error)).toBe('NotAllowedError: Permission denied');
  });

  it('adds the failed constraint when the error carries one', () => {
    const error = Object.assign(new Error('Invalid constraint'), {
      name: 'OverconstrainedError',
      constraint: 'deviceId',
    });
    expect(describeError(error)).toBe('OverconstrainedError: Invalid constraint (constraint: deviceId)');
  });

  it('adds HTTP status and error code for API client errors', () => {
    const error = Object.assign(new Error('Unsupported audio type'), {
      name: 'ApiClientError',
      statusCode: 400,
      errorCode: 'unsupported_audio',
    });
    expect(describeError(error)).toBe('ApiClientError: Unsupported audio type [HTTP 400 unsupported_audio]');
  });

  it('handles non-Error throwables', () => {
    expect(describeError('boom')).toBe('boom');
    expect(describeError({ name: 'Weird', message: 'thing' })).toBe('Weird: thing');
    expect(describeError(42)).toBe('42');
    expect(describeError(new Error(''))).toBe('Error: (no message)');
  });
});

describe('formatVoiceInputReport', () => {
  it('lays out stage, notice, error, environment, clip and timeline', () => {
    const text = formatVoiceInputReport({
      stage: 'recording',
      message: 'Nothing was recorded.',
      error: 'Error: capture track ended',
      environment: [['browser', 'Mozilla/5.0 (iPhone)'], ['secure context', 'yes']],
      timeline: ['+0ms getUserMedia default device by id', '+1200ms recording (live signal)'],
      clip: { mimeType: 'audio/mp4', bytes: 0, durationMs: 2500, chunks: 0 },
      at: new Date('2026-10-12T20:00:00.000Z'),
    });
    expect(text).toBe([
      'Quest voice input report',
      'when: 2026-10-12T20:00:00.000Z',
      'stage: recording (recording)',
      'notice: Nothing was recorded.',
      'error: Error: capture track ended',
      'browser: Mozilla/5.0 (iPhone)',
      'secure context: yes',
      'clip: audio/mp4 0 bytes, 2.5 s, 0 chunks',
      'timeline:',
      '  +0ms getUserMedia default device by id',
      '  +1200ms recording (live signal)',
    ].join('\n'));
  });

  it('marks a missing error and an empty timeline explicitly', () => {
    const text = formatVoiceInputReport({
      stage: 'open_microphone',
      message: 'Voice input is not available in this browser.',
      error: '',
      environment: [],
      timeline: [],
      at: new Date('2026-10-12T20:00:00.000Z'),
    });
    expect(text).toContain('stage: open_microphone (opening the microphone)');
    expect(text).toContain('error: (none)');
    expect(text.endsWith('timeline:\n  (empty)')).toBe(true);
    expect(text).not.toContain('clip:');
  });
});
