// useSaveStatus: the saving -> saved / error -> idle cycle behind a settings
// section's Save button.
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useSaveStatus } from './useSaveStatus';

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('useSaveStatus', () => {
  it('starts idle with no error', () => {
    const { result } = renderHook(() => useSaveStatus());
    expect(result.current.saveStatus).toBe('idle');
    expect(result.current.saveError).toBe('');
  });

  it('shows saving while the save runs, then saved for two seconds', async () => {
    const { result } = renderHook(() => useSaveStatus());
    let finish: () => void = () => {};
    const save = () => new Promise<void>((resolve) => { finish = resolve; });

    let pending: Promise<void> = Promise.resolve();
    act(() => { pending = result.current.runSave(save, 'failed:'); });
    expect(result.current.saveStatus).toBe('saving');

    await act(async () => { finish(); await pending; });
    expect(result.current.saveStatus).toBe('saved');

    act(() => { vi.advanceTimersByTime(1999); });
    expect(result.current.saveStatus).toBe('saved');
    act(() => { vi.advanceTimersByTime(1); });
    expect(result.current.saveStatus).toBe('idle');
  });

  it('shows the failure message for three seconds and logs it', async () => {
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { result } = renderHook(() => useSaveStatus());
    const failure = new Error('Label limit reached');

    await act(async () => {
      await result.current.runSave(() => Promise.reject(failure), 'Failed to save Gmail settings:');
    });
    expect(result.current.saveStatus).toBe('error');
    expect(result.current.saveError).toBe('Label limit reached');
    expect(logged).toHaveBeenCalledWith('Failed to save Gmail settings:', failure);

    act(() => { vi.advanceTimersByTime(2999); });
    expect(result.current.saveStatus).toBe('error');
    act(() => { vi.advanceTimersByTime(1); });
    expect(result.current.saveStatus).toBe('idle');
  });

  it('falls back to a generic message for a non-Error failure', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const { result } = renderHook(() => useSaveStatus());
    await act(async () => {
      await result.current.runSave(() => Promise.reject('nope'), 'failed:');
    });
    expect(result.current.saveError).toBe('Failed to save');
  });

  it('clears the previous error when a new save starts', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const { result } = renderHook(() => useSaveStatus());
    await act(async () => {
      await result.current.runSave(() => Promise.reject(new Error('first')), 'failed:');
    });
    await act(async () => {
      await result.current.runSave(() => Promise.resolve(), 'failed:');
    });
    expect(result.current.saveStatus).toBe('saved');
    expect(result.current.saveError).toBe('');
  });

  it('a second save restarts the reset timer', async () => {
    const { result } = renderHook(() => useSaveStatus());
    await act(async () => { await result.current.runSave(() => Promise.resolve(), 'failed:'); });
    act(() => { vi.advanceTimersByTime(1500); });
    await act(async () => { await result.current.runSave(() => Promise.resolve(), 'failed:'); });
    act(() => { vi.advanceTimersByTime(1500); });
    expect(result.current.saveStatus).toBe('saved');
    act(() => { vi.advanceTimersByTime(500); });
    expect(result.current.saveStatus).toBe('idle');
  });
});
