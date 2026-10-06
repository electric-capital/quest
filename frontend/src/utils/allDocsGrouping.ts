/**
 * Pure list derivation for the All Docs view (components/docs/DocsListView):
 * client-side search, the "Your docs" + "Shared with you" + per-project
 * grouping, and the human-readable size column. Kept free of React so it is
 * unit-tested in allDocsGrouping.test.ts like utils/sidebarItems.ts.
 */

import type { Doc, Project } from '../api/types';
import { sharedByLabel, sharedByTitle, sharedOwnerSearchText } from './docSharing';

/** Key of the always-present "Your docs" group. */
export const USER_DOCS_GROUP_KEY = 'user';

/** Key of the "Shared with you" group (docs other people shared). */
export const SHARED_DOCS_GROUP_KEY = 'shared';

/** Group key of one project's docs. */
export function projectGroupKey(projectId: string): string {
  return `project:${projectId}`;
}

export type DocGroup = {
  key: string;
  label: string;
  // The project a project group belongs to; null for "Your docs", "Shared
  // with you" and for a project id the loaded project list does not know.
  project: Project | null;
  // Null for "Your docs" and "Shared with you"; set for every project group
  // (known or not).
  projectId: string | null;
  docs: Doc[];
};

/**
 * Docs whose title or description -- or, for a doc shared with the viewer,
 * its owner's name or email -- contains `query` (trimmed, case-insensitive
 * substring). An empty / whitespace query keeps them all.
 */
export function filterDocs(docs: Doc[], query: string): Doc[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return docs;
  return docs.filter(
    (doc) =>
      doc.title.toLowerCase().includes(needle) ||
      (doc.description ?? '').toLowerCase().includes(needle) ||
      sharedOwnerSearchText(doc).toLowerCase().includes(needle),
  );
}

/**
 * "Your docs" first (always present, even when empty), then -- when
 * `sharedDocs` is passed -- "Shared with you" (present even when empty; the
 * view decides whether an empty one shows), then one group per project id
 * in `byProject` that has at least one doc, ordered by project name
 * (case-insensitive, id as the tiebreak). A project id missing from
 * `projects` is labelled "Project". Docs keep the order they arrived in
 * (the server's newest-updated first).
 */
export function groupDocs(
  userDocs: Doc[],
  byProject: Record<string, Doc[]>,
  projects: Project[],
  sharedDocs?: Doc[],
): DocGroup[] {
  const projectsById = new Map(projects.map((p) => [p.id, p]));
  const projectGroups: DocGroup[] = Object.entries(byProject)
    .filter(([, docs]) => docs.length > 0)
    .map(([projectId, docs]) => {
      const project = projectsById.get(projectId) ?? null;
      return {
        key: projectGroupKey(projectId),
        label: project?.name ?? 'Project',
        project,
        projectId,
        docs,
      };
    });
  projectGroups.sort((a, b) => {
    const byName = a.label.toLowerCase().localeCompare(b.label.toLowerCase());
    if (byName !== 0) return byName;
    return (a.projectId ?? '') < (b.projectId ?? '') ? -1 : 1;
  });
  const groups: DocGroup[] = [
    { key: USER_DOCS_GROUP_KEY, label: 'Your docs', project: null, projectId: null, docs: userDocs },
  ];
  if (sharedDocs) {
    groups.push({
      key: SHARED_DOCS_GROUP_KEY,
      label: 'Shared with you',
      project: null,
      projectId: null,
      docs: sharedDocs,
    });
  }
  return [...groups, ...projectGroups];
}

/**
 * The scope column: "Shared by <owner>" for a doc someone shared with the
 * viewer (user or project doc alike: the viewer cannot open the owner's
 * project), else the project's name for a project doc ("Project" when the
 * project is unknown) and "Your doc" for the viewer's own user doc.
 */
export function docScopeLabel(doc: Doc, project: Project | null): string {
  if (doc.shared_with_me) return sharedByLabel(doc);
  if (doc.project_id) return project?.name ?? 'Project';
  return 'Your doc';
}

/**
 * The scope cell's tooltip: "Shared by <owner> · can edit / can view" for a
 * shared doc, none otherwise.
 */
export function docScopeTitle(doc: Doc): string | undefined {
  return doc.shared_with_me ? sharedByTitle(doc) : undefined;
}

const KB = 1024;
const MB = 1024 * 1024;

/**
 * Body size plus embedded images: "340 B", "1.2 KB", "2.0 MB",
 * "1.2 KB + 3 images", "0 B + 1 image".
 */
export function formatDocSize(contentSize: number, assetCount: number): string {
  const bytes = Number.isFinite(contentSize) && contentSize > 0 ? Math.floor(contentSize) : 0;
  let size: string;
  if (bytes < KB) {
    size = `${bytes} B`;
  } else if (bytes < MB && (bytes / KB).toFixed(1) !== '1024.0') {
    size = `${(bytes / KB).toFixed(1)} KB`;
  } else {
    size = `${(bytes / MB).toFixed(1)} MB`;
  }
  const images = Number.isFinite(assetCount) && assetCount > 0 ? Math.floor(assetCount) : 0;
  if (images === 0) return size;
  return `${size} + ${images} ${images === 1 ? 'image' : 'images'}`;
}
