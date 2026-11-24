# AutoProjects Frontend

## Overview

The UI of [AutoProjects](autoprojects.md) at `/autoprojects`: the sidebar section, the AutoProjects list, New AutoProject, and the AutoProject page with its Brief, AutoProject files, Docs and tables and Updates cards, its in-flow composer and its detail panel (a run's transcript or the brief History). It follows the Quest Docs and Quest Tables views in shape (the URL is the only state, a full main-pane takeover, a gate-closed fallback), so this doc covers the files and the rules that span them; each component's header comment describes its own behaviour. The routes it calls and the start sequence are in the [AutoProjects API](../api/autoprojects-api.md). Paths are under `frontend/src/` unless noted. User-visible text calls a run an "update".

## Key Files

| File | Role |
|------|------|
| `api/autoprojectsApi.ts` | Client for `/app/api/autoprojects*`, `AUTOPROJECTS_FEATURE`, the hand-mirrored caps and page sizes, and the accessors for the error extras (`current`, `active_run`, `cap` / `actual`) |
| `api/types.ts` | `AutoProject`, `AutoProjectListRow`, `AutoProjectRun`, the question, brief-version and transcript-row types |
| `services/autoprojectRuns.ts` / `services/sendAck.ts` | The two-step run start and Stop; the send-receipt pieces shared with `WebSocketManager` |
| `utils/autoprojectsRoute.ts` | `parseAutoProjectsRoute()` and the path builders |
| `hooks/useAutoProjects.ts` / `hooks/useAutoProject.ts` / `hooks/useAutoProjectRuns.ts` / `hooks/useAutoProjectRun.ts` | One list (unarchived or archived); one AutoProject's detail; its runs, paged and merged; the run open in the panel |
| `hooks/useAutoProjectRunActivity.ts` | The active run's activity label, from its conversation's live events |
| `hooks/useAutoProjectUnread.ts` + `store/unreadStore.ts` | The AutoProject unread dot and the viewed-AutoProject acknowledgement |
| `components/sidebar/AutoProjectsSection.tsx` + `utils/sidebarAutoProjects.ts` | The sidebar block and its pure row derivation |
| `components/autoprojects/AutoProjectsMain.tsx` | The main-pane switch over the parsed route, behind the gate |
| `components/autoprojects/AutoProjectsListView.tsx`, `NewAutoProjectPage.tsx` | The list (and the archived list) and New AutoProject |
| `components/autoprojects/AutoProjectPage.tsx`, `AutoProjectMainColumn.tsx`, `AutoProjectHeader.tsx` | The page, its main column (composer state, scrolling, cards, arrivals) and its title unit |
| `components/autoprojects/AutoProjectOutline.tsx` | The page outline: the table of contents left of the column, its DOM scan and its width rule |
| `components/autoprojects/AutoProjectBrief.tsx`, `AutoProjectBriefEditor.tsx`, `AutoProjectBriefHistory.tsx`, `AutoProjectBriefControls.tsx` | The Brief card, its in-place editor, the History view and the shared counters and confirms |
| `components/autoprojects/AutoProjectArtifacts.tsx` | The Docs and tables card: the Quest Docs and Quest Tables the AutoProject owns, New doc and New table |
| `components/autoprojects/AutoProjectUpdates.tsx`, `AutoProjectRunEntry.tsx`, `AutoProjectActivityLine.tsx`, `AutoProjectQuestionCard.tsx`, `AutoProjectRunError.tsx` | The Updates card and one update: prompt, activity line, question cards, approval and connect cards, result or state line |
| `components/autoprojects/AutoProjectDetailPanel.tsx`, `AutoProjectRunPanel.tsx`, `AutoProjectRunPanelContext.ts`, `AutoProjectQuestionMessage.tsx` | The detail panel, the run view with its tabs, and the transcript's question rows |
| `components/autoprojects/AutoProjectComposer.tsx`, `autoprojectComposerSend.ts`, `autoprojectComposerState.ts` + `store/autoprojectDrafts.ts` | The shared composer set up for AutoProjects, the submission helper, the composer state machine, the kept drafts |
| `components/autoprojects/autoprojectArrival.ts`, `autoprojectUpdatesOrder.ts` | The arrival animation and the stored sort order |
| `components/autoprojects/AutoProjectsGateClosed.tsx` | Shown on an `/autoprojects*` URL while the gate is closed |
| `App.tsx`, `components/MobileShell.tsx`, `components/Sidebar.tsx` | Route rendering, the `/chats/<run id>` redirect, sidebar mounting |

## Routes

`parseAutoProjectsRoute()` turns the URL into one of four views: the list (`/autoprojects`, `?archived=true` for the archive), New AutoProject (`/autoprojects/new`; `new` is never an id), the AutoProject page (`/autoprojects/<id>`) and the page with a run open in its detail panel (`/autoprojects/<id>/runs/<conversation id>`). As for docs and tables, the URL is the only state: `App.tsx` and `MobileShell.tsx` parse it on every render and render `AutoProjectsMain` as a full main-pane takeover with no RightPanel. The run's conversation is deliberately not the router's `conversationId` param, so a run never becomes the active sidebar conversation. `AutoProjectsMain` keys the page by AutoProject id only, so opening, switching or closing a run keeps the page and its state.

A run opened by its conversation URL (`/chats/<run id>`, e.g. from a doc's "written by" link) is redirected with `replace` to its run URL once `ChatPanel` reports the detail's `autoproject_id` (`onAutoProjectIdLoaded`, `handleAutoProjectIdLoaded` in `App.tsx`), like the [project redirect](frontend.md#project-redirect). While `autoprojects` is missing from `enabledFeatures`, an AutoProjects route renders `AutoProjectsGateClosed`, nothing is fetched, the sidebar has no section, and a run URL stays a read-only transcript on `/chats/<id>`. `serve_spa_autoprojects` in `quest.py` serves the deep links.

## Lists and New AutoProject

`AutoProjectsSection` sits in the main sidebar panel between Projects and Docs (AutoProjects are top-level, so the project drill-down has none). Its rows come from `useAutoProjects` and carry the unread dot and one status slot: working while a run runs, "Needs you" while one waits on the user, otherwise the last activity. `AutoProjectsListView` is the full list and the archive, with Archive (disabled while a run is active), Unarchive and Delete.

New AutoProject is an empty page whose composer creates the AutoProject on the first Send (`NewAutoProjectPage`): `POST /autoprojects` with the prompt, so the server cuts the title from it and a run may still rename it, then the run start, then navigation to the page. A model refusal on the create is retried without the model, so it never blocks the AutoProject; a run that did not start rides the router state to the page as a notice.

## AutoProject Page

`AutoProjectPage` lays out one readable-width main column and, while a detail view is open, the detail panel beside it. The main column (`AutoProjectMainColumn`) is the header, then the Brief card, the AutoProject files card, the Docs and tables card, the composer in the page flow (not pinned) and the Updates card, in one scroller. Rules that span its parts:

- **Opens at the top**, on the brief. A jump button in the header ("Latest" / "Brief") moves between the brief and the newest update and carries the working or unread mark while the newest update is off screen.
- **Outline.** While the scroller is wide enough, `AutoProjectOutline` sits sticky left of the column as a table of contents, the pair centred so the space left of the outline equals the space right of the column: one entry per card (AutoProject files as "Files"), the rendered brief's h1 to h3 under Brief, and the first ten updates in the shown order (number and prompt) under Updates. An entry scrolls its target to the top and the entry the reader is in is marked. It is scanned from the column's DOM (re-scanned on mutations), so it lists exactly what is rendered: no entry for an absent card, no brief headings while the brief is being edited. Below that width (phones, a small window, the detail panel open beside a narrow column) there is no outline.
- **Updates order.** Recent first (the default, so a new update appears right under the composer) or oldest first, one preference for every AutoProject kept in `localStorage` (`autoprojectUpdatesOrder.ts`). "Show earlier updates" sits at the old end; oldest first keeps the previously first update in place while an older page is prepended.
- **Composer states** (`deriveAutoProjectComposerState()`): `start` (Send starts the next run), `answer` (the active run waits on a question: Send answers the oldest pending one, with a label quoting it and a link to its card), `locked` (the run works or waits only on cards: disabled, Stop offered) and `archived` (no composer, an Unarchive notice). The active run comes from the loaded runs, with the detail's `active_run` as the fallback while that run is not loaded.
- **Starting.** A start shows an optimistic entry until the run appears in the runs list; sending a prompt or an answer brings the newest update into view.
- **Cards.** `open_requests` lists only open cards, so the column keeps every card it has seen for an active run (each flips itself to its resolved state) and drops them once the run finished.
- **Arrival animation.** An update and its result open and fade in (`autoprojectArrival.ts`) only for transitions the page saw live, each claimed once, so nothing animates on load, paging, a sort change or navigation; under `prefers-reduced-motion` only the highlight's colour fades.
- **Model.** The composer is seeded with the AutoProject's model; a user's pick is PATCHed back as the AutoProject's default, and a refused PATCH is only logged, since the run still uses the composer's pick.

The **Brief** card renders the markdown with its authorship byline, edits in place (`AutoProjectBriefEditor`: a 409 `stale_brief` opens a merge box above the untouched draft; Save never sends a version whose text the user has not been shown) and opens History in the detail panel. The **AutoProject files** card is a compact `FileBrowser` over the AutoProject space. The **Docs and tables** card (`AutoProjectArtifacts`) lists the docs and tables the AutoProject owns ([AutoProjects -- Docs and Tables](autoprojects.md#docs-and-tables)) as one newest-first list linking to the doc and table viewers. It reads `useDocs` and `useTables` with `autoprojectId`, so it follows `doc_list_changed` and `table_list_changed`; each kind shows only while its feature is in `enabledFeatures`, and with neither the card is absent. New doc and New table open `NewDocModal` and `NewTableDialog` with the AutoProject as their fixed location (no Location select; the request carries `autoproject_id`); an archived AutoProject keeps the list and has no buttons. In the viewers, `DocHeader` and `TableHeader` show an AutoProject chip linking back to the page.

## Detail Panel

`AutoProjectDetailPanel` shows one view at a time: beside the main column on desktop, full screen with "Back to AutoProject" on phones. Escape closes it unless a modal or menu owns the key; an edited draft asks before it is discarded.

- **Run view** (`AutoProjectRunPanel`, opened by "View details" or the run URL). `useAutoProjectRun` finds the run by its conversation by paging the AutoProject's runs (there is no by-conversation route) and re-reads it on every `autoproject_changed`; anything that is not a run of this AutoProject shows a not-found note and mounts no transcript. Tabs: **Transcript** (`ChatPanel`, read-only through the `autoproject` origin, its own header suppressed; `AutoProjectRunPanelContext` lets the transcript's question rows answer through the AutoProject), **Handoff notes** (only for a finished run that left notes) and **Chat Files** (the run's workspace, with Copy / Move to the AutoProject files). "View details" pushes the run URL and closing goes back when the page pushed it, so Back and close agree.
- **Brief history** (`AutoProjectBriefHistory`, plain component state, no URL): the version list, one version as markdown or as a word diff against the current brief, and Restore, which sends the `brief_version` frozen when its confirm opened, so a write landing behind the dialog is a 409, never a silent overwrite.

Copy and Move between the run's Chat Files and the AutoProject files card come from one `useWorkspaceCopy` over the run open in the panel, so the AutoProject files card has copy actions only while a run is open.

## Activity and Subscriptions

The page is not a `ChatPanel`, so `useAutoProjectRunActivity` follows the active run's conversation straight off the persistent socket: it takes a subscription hold, applies catch-up rows, fetches the tail after each `message_appended`, and derives one short label (the tool's `intent_message`, "Thinking", or the pending question or card); its header comment lists the label rules. It never touches `webSocketManager`, the conversation store or `useConversation`.

The run panel's `ChatPanel` holds the same conversation through `useConversation`, which never lets go. `PersistentWebSocket` therefore counts holders per conversation: every `subscribe` adds one, `unsubscribe` removes one, and the wire `unsubscribe` goes out only when the last holder leaves, so the activity line's cleanup never cuts the panel's stream ([Realtime -- Subscription Protocol](realtime.md#subscription-protocol)).

Everything else follows the two per-user globals: `useAutoProjects` re-fetches (debounced) on `autoproject_list_changed`, `useAutoProject` and `useAutoProjectRuns` on `autoproject_changed` for their own AutoProject only, and the brief editor resets its draft only when `brief_version` moved. Globals published while the socket was down are lost, so these hooks and the file browser also re-fetch on `persistentWebSocket.onReconnect`. Fetches coalesce as in `useTable`: one in flight, plus one dirty re-run. None re-fetch an AutoProject this tab is deleting.

## Read State

The AutoProject counts as seen only while its newest update is actually on screen (not covered by the phone's full-screen panel) or the panel shows the newest run; an AutoProject without runs counts as seen. `useMarkAutoProjectViewed` reports it to `unreadStore`, which acknowledges in a focused window through `POST /autoprojects/{id}/read`, in separate maps from the conversation seqs. Opening the page at the top therefore never marks an unseen update read. The seqs reach the store from list rows, the detail and the read response; there is no AutoProject unread event ([AutoProjects -- Unread](autoprojects.md#unread)).

## Composer

`AutoProjectComposer` is the shared `Composer.tsx` bound to a draft key (`AUTOPROJECT_DRAFT_KEY` on New AutoProject, `autoprojectDraftKey(id)` on a page, `constants/drafts.ts`), never to a run conversation, because every Send starts a fresh one: no provider lock, no flags (`hideFlags`), no kept "loaded" skills, pasted images uploaded only once the run conversation exists (`deferImageUpload`). The unsent text is kept per draft key in `store/autoprojectDrafts.ts` (memory mirrored to `sessionStorage`). `startAutoProjectRunFromComposer()` uploads attachments into the run conversation before the send; a failed upload request, or every pasted image rejected, aborts the start, so a prompt never goes out without the pictures it talks about.

`startAutoProjectRun()` settles on the server's direct answer to the sending socket and never retries on the same conversation: a failed or unconfirmed send spends it ([AutoProjects API -- Starting a Run](../api/autoprojects-api.md#starting-a-run)).

## Shared Components

The feature extends these shared pieces rather than forking them:

- **`Composer.tsx`**: `hideFlags`, `placeholder`, `initialText` / `onTextChange` for the kept drafts, and a refocus after a run that never scrolls and leaves another focused input alone.
- **`ChatPanel.tsx`**: `autoproject` joins the read-only origins (only a notice line in place of the composer, linking to the AutoProject while the gate is open); `header` replaces or hides the conversation header; `onAutoProjectIdLoaded` reports a run for the redirect.
- **`Message.tsx`**: `autoproject_question` rows render `AutoProjectQuestionMessage` and `autoproject_nudge` rows a notice ([Frontend -- Message Component](frontend.md#message-component-with-markdown-rendering) covers the space references in markdown).
- **`FileBrowser.tsx`**: an `autoproject` source (`autoprojectSource()` in `api/fileApi.ts`) with its routes and event filter, and the `compact` variant for a card inside a page column.
- **`useWorkspaceCopy`**: the run-to-AutoProject pair of spaces beside the project pair.
- **`RequestsView.tsx`**: an AutoProject run's request links to the AutoProject page while open, and to the run view once resolved.
- **`ToolUseMessage.tsx`**: the two run loop tools read as steps of the run, with the log entry's text fields as labelled blocks.

## Phone Layout

The page is one column in the same order; a long brief and a long prompt are clamped behind a measured "Show full" toggle (`useClampOverflow`), and each detail view opens full screen with a back button. `MobileShell` closes its drawers whenever the AutoProjects route changes and offers no workspace drawer on these views.

## Design Decisions

**Why the brief in the main column and runs as updates under it?**
The brief is the state of the work and what the user edits; a run's transcript is a debugging view. Showing results as updates on the AutoProject, with transcripts one click away in the panel, keeps the page about the work rather than about conversations.

**Why a composer bound to a draft key instead of a conversation?**
Each Send needs a fresh run conversation, which exists only after the click. Binding to a stable key keeps the model, skills and draft per AutoProject without locking a provider to a conversation that never takes a second message.

**Why count subscription holders instead of never unsubscribing?**
The activity line follows a run only while it is active and must let go afterwards, while the run panel's `ChatPanel` keeps the same conversation. Counting holders lets both coexist without one tearing down the other's stream.
