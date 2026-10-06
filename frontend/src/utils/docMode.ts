/**
 * Doc mode display rule, shared by every DocModeBadge call site (sidebar doc
 * rows, the All Docs list and its "Mode" column label, the doc viewer
 * header, the New Doc modal's inherited-mode line).
 *
 * User docs are always private; a public doc exists only inside a public
 * project (it inherits the project's mode). Private is therefore the
 * unremarkable default and carries no badge anywhere: only a public doc is
 * labelled, so its exposure to public conversations is never hidden.
 */

import type { DocMode } from '../api/types';

/** Whether a doc's DocModeBadge should render: only for a public doc. */
export function shouldShowDocModeBadge(mode: DocMode): boolean {
  return mode === 'public';
}
