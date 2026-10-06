/**
 * Pure list derivation for the All Docs view (components/docs/DocsListView):
 * client-side search, the "Your docs" + per-project grouping, and the
 * human-readable size column. Kept free of React so it is unit-tested in
 * allDocsGrouping.test.ts like utils/sidebarItems.ts.
 */

import type { Doc, Project } from '../api/types';

/** Key of the always-present "Your docs" group. */
export const USER_DOCS_GROUP_KEY = 'user';

/** Group key of one project's docs. */
export function projectGroupKey(projectId: string): string {
  return `project:${projectId}`;
}

export type DocGroup = {
  key: string;
  label: string;
  // The project a project group belongs to; null for "Your docs" and for a
  // project id the loaded project list does not know.
  project: Project | null;
  // Null for "Your docs"; set for every project group (known or not).
  projectId: string | null;
  docs: Doc[];
};

/**
 * Docs whose title or description contains `query` (trimmed,
 * case-insensitive substring). An empty / whitespace query keeps them all.
 */
export function filterDocs(docs: Doc[], query: string): Doc[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return docs;
  return docs.filter(
    (doc) =>
      doc.title.toLowerCase().includes(needle) ||
      (doc.description ?? '').toLowerCase().includes(needle),
  );
}

/**
 * "Your docs" first (always present, even when empty), then one group per
 * project id in `byProject` that has at least one doc, ordered by project
 * name (case-insensitive, id as the tiebreak). A project id missing from
 * `projects` is labelled "Project". Docs keep the order they arrived in
 * (the server's newest-updated first).
 */
export function groupDocs(
  userDocs: Doc[],
  byProject: Record<string, Doc[]>,
  projects: Project[],
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
  return [
    { key: USER_DOCS_GROUP_KEY, label: 'Your docs', project: null, projectId: null, docs: userDocs },
    ...projectGroups,
  ];
}

/**
 * The scope column: the project's name for a project doc ("Project" when the
 * project is unknown), "Your doc" for a user doc the viewer owns, and
 * "Shared with you" for someone else's user doc (only the owner may delete,
 * so `access.can_delete` doubles as the ownership bit).
 */
export function docScopeLabel(doc: Doc, project: Project | null): string {
  if (doc.project_id) return project?.name ?? 'Project';
  return doc.access?.can_delete === false ? 'Shared with you' : 'Your doc';
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
