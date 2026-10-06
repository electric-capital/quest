/**
 * Doc mode display rules that depend on the user's feature gates.
 *
 * Public docs exist only alongside public projects: while the
 * `public_projects` gate is closed for the user, every new doc is private
 * and a "Private" badge on every row would label a distinction the user
 * cannot act on. A PUBLIC doc keeps its badge regardless (a leftover public
 * doc from before the gate closed must never look private).
 */

import type { DocMode } from '../api/types';

/** Feature-gate name of public projects (and with them, public docs). */
export const PUBLIC_PROJECTS_FEATURE = 'public_projects';

/** True when the user may create public projects / public docs. */
export function isPublicProjectsEnabled(enabledFeatures: readonly string[]): boolean {
  return enabledFeatures.includes(PUBLIC_PROJECTS_FEATURE);
}

/**
 * Whether a doc's DocModeBadge should render: always for a public doc, and
 * for a private doc only while public docs are possible for the user.
 */
export function shouldShowDocModeBadge(mode: DocMode, publicProjectsEnabled: boolean): boolean {
  return mode === 'public' || publicProjectsEnabled;
}
