/**
 * A `<button>` whose width glides to fit a changed label instead of
 * snapping (see hooks/useAnimatedWidth.ts). Drop-in for a plain button
 * whose text swaps while a request is in flight ("Approve" ->
 * "Processing..."); the incoming label fades in while the box catches up.
 */

import type { ButtonHTMLAttributes } from 'react';
import { useAnimatedWidth } from '../hooks/useAnimatedWidth';
import './AnimatedWidthButton.css';

export function AnimatedWidthButton({
  children,
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement>) {
  const ref = useAnimatedWidth<HTMLButtonElement>(children);
  // Re-keying the label span on a text swap restarts its fade-in.
  const labelKey = typeof children === 'string' ? children : undefined;
  return (
    <button ref={ref} className={`animated-width-btn${className ? ` ${className}` : ''}`} {...props}>
      <span key={labelKey} className="animated-width-btn-label">{children}</span>
    </button>
  );
}
