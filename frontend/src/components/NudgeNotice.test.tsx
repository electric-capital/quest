// The transcript notice of the follow-up turn the server runs when a routine
// run or an AutoProject run ended without its completion tool: one line per
// kind with its own classes, the time, and the exact nudge text the model
// received in the tooltip.
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import type { MessageContent } from '../api/types';
import { formatTimestamp } from '../utils/formatters';
import { MessageContentRenderer } from './Message';

afterEach(cleanup);

describe('NudgeNotice', () => {
  it.each([
    [
      'routine',
      'routine_nudge',
      'Routine run ended without calling routine_completed — asked the model to check its work and finish',
    ],
    [
      'autoproject',
      'autoproject_nudge',
      'The update did not finish — asked the model to call complete_autoproject_run and hand off',
    ],
  ] as const)('renders the %s nudge with its text, time and the prompt in the tooltip', (kind, type, text) => {
    const message = {
      type,
      content: 'Call the completion tool now.',
      timestamp: '2026-11-10T10:00:00',
    } as MessageContent;
    const { container } = render(<MessageContentRenderer message={message} />);
    const notice = container.querySelector(`.message-${kind}-nudge`) as HTMLElement;
    expect(notice).not.toBeNull();
    expect(notice.getAttribute('title')).toBe('Call the completion tool now.');
    expect(screen.getByText(text).className).toBe(`${kind}-nudge-text`);
    expect(notice.querySelector(`.${kind}-nudge-timestamp`)!.textContent).toBe(
      formatTimestamp('2026-11-10T10:00:00'),
    );
  });

  it('renders a nudge without content or time without a tooltip or a time', () => {
    const { container } = render(<MessageContentRenderer message={{ type: 'autoproject_nudge' } as MessageContent} />);
    const notice = container.querySelector('.message-autoproject-nudge') as HTMLElement;
    expect(notice.hasAttribute('title')).toBe(false);
    expect(notice.querySelector('.autoproject-nudge-timestamp')!.textContent).toBe('');
  });
});
