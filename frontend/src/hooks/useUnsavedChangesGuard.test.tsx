// useUnsavedChangesGuard: which link clicks are intercepted
// (interceptedLinkTarget), the capture-phase interception itself, and the
// beforeunload prompt -- all only while `when` is true.
import { afterEach, describe, expect, it, vi } from 'vitest';
import type React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { interceptedLinkTarget, useUnsavedChangesGuard } from './useUnsavedChangesGuard';

const LOCATION = {
  href: 'http://localhost:3000/docs/d1',
  origin: 'http://localhost:3000',
  pathname: '/docs/d1',
  search: '',
};

/** A click event on a fresh anchor (attributes applied), dispatched nowhere. */
function clickOn(attrs: Record<string, string>, init: MouseEventInit = {}): MouseEvent {
  const anchor = document.createElement('a');
  for (const [name, value] of Object.entries(attrs)) anchor.setAttribute(name, value);
  const inner = document.createElement('span');
  anchor.appendChild(inner);
  document.body.appendChild(anchor);
  const event = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0, ...init });
  Object.defineProperty(event, 'target', { value: inner });
  anchor.remove();
  return event;
}

interface ProbeProps {
  when: boolean;
  onBlocked: (to: string) => void;
  /** Sees every click the guard let through (then stops jsdom navigating). */
  onPassed?: (label: string) => void;
}

function Probe({ when, onBlocked, onPassed = () => {} }: ProbeProps) {
  useUnsavedChangesGuard({ when, onBlockedNavigation: onBlocked });
  const passed = (label: string) => (event: React.MouseEvent) => {
    event.preventDefault();
    onPassed(label);
  };
  return (
    <>
      <a href="/chats/c1?x=1#m2" onClick={passed('chat')}>Chat</a>
      <a href="/chats/c1" target="_blank" onClick={passed('new tab')}>New tab</a>
    </>
  );
}

describe('interceptedLinkTarget', () => {
  it('returns path + search + hash for a plain same-origin link click', () => {
    expect(interceptedLinkTarget(clickOn({ href: '/chats/c1?x=1#m' }), LOCATION)).toBe('/chats/c1?x=1#m');
    expect(interceptedLinkTarget(clickOn({ href: '/docs', target: '_self' }), LOCATION)).toBe('/docs');
  });

  it('lets through modified, non-left, other-window, download and external clicks', () => {
    const link = { href: '/chats/c1' };
    expect(interceptedLinkTarget(clickOn(link, { metaKey: true }), LOCATION)).toBeNull();
    expect(interceptedLinkTarget(clickOn(link, { ctrlKey: true }), LOCATION)).toBeNull();
    expect(interceptedLinkTarget(clickOn(link, { shiftKey: true }), LOCATION)).toBeNull();
    expect(interceptedLinkTarget(clickOn(link, { altKey: true }), LOCATION)).toBeNull();
    expect(interceptedLinkTarget(clickOn(link, { button: 1 }), LOCATION)).toBeNull();
    expect(interceptedLinkTarget(clickOn({ ...link, target: '_blank' }), LOCATION)).toBeNull();
    expect(interceptedLinkTarget(clickOn({ ...link, download: '' }), LOCATION)).toBeNull();
    expect(interceptedLinkTarget(clickOn({ href: 'https://example.com/x' }), LOCATION)).toBeNull();
  });

  it('lets through a link to the current page (e.g. a #hash) and non-link clicks', () => {
    expect(interceptedLinkTarget(clickOn({ href: '#section' }), LOCATION)).toBeNull();
    expect(interceptedLinkTarget(clickOn({ href: '/docs/d1#top' }), LOCATION)).toBeNull();
    expect(interceptedLinkTarget(clickOn({}), LOCATION)).toBeNull(); // <a> without href
  });
});

describe('useUnsavedChangesGuard', () => {
  afterEach(() => {
    cleanup();
  });

  it('intercepts in-app link clicks while active, before React sees them', () => {
    const onBlocked = vi.fn();
    const onPassed = vi.fn();
    const { rerender } = render(<Probe when={true} onBlocked={onBlocked} onPassed={onPassed} />);
    expect(fireEvent.click(screen.getByText('Chat'))).toBe(false);
    expect(onBlocked).toHaveBeenCalledWith('/chats/c1?x=1#m2');
    expect(onPassed).not.toHaveBeenCalled();

    // A new-tab link is not ours to stop.
    fireEvent.click(screen.getByText('New tab'));
    expect(onPassed).toHaveBeenCalledWith('new tab');
    expect(onBlocked).toHaveBeenCalledTimes(1);

    rerender(<Probe when={false} onBlocked={onBlocked} onPassed={onPassed} />);
    fireEvent.click(screen.getByText('Chat'));
    expect(onPassed).toHaveBeenCalledWith('chat');
    expect(onBlocked).toHaveBeenCalledTimes(1);
  });

  it('asks the browser to confirm unloading only while active', () => {
    const { rerender } = render(<Probe when={true} onBlocked={() => {}} />);
    const active = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(active);
    expect(active.defaultPrevented).toBe(true);

    rerender(<Probe when={false} onBlocked={() => {}} />);
    const inactive = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(inactive);
    expect(inactive.defaultPrevented).toBe(false);
  });
});
