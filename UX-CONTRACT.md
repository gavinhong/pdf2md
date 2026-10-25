# PDF2MD interaction contract

Product authority: REQUIREMENTS.md v0.4 (application 0.2.1) and TECHNICAL_DESIGN.md. This document records
observable UI consequences. Import is read-only; removing a queue entry never deletes
source or exported files. There are no accounts, permissions, billing or network services.

## Canonical UI Map

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
| --- | --- | --- | --- | --- |
| Table Selection | task-list document button | REQUIREMENTS.md FR-02 | one preview selection, up to 100 tasks | tests/ui.spec.ts |
| Select/Listbox | native select in settings | DESIGN.md | native popup owned by OS | tests/ui.spec.ts |
| Form | settings-form with inline errors | REQUIREMENTS.md FR-03 | native inputs, no browser validation bubbles | tests/ui.spec.ts |
| Scrollbar | global stylesheet | DESIGN.md | queue and preview internal scroll; narrow screen document scroll | tests/ui.spec.ts |
| Toast | shared notify live region | this contract | info, success, warning, error | tests/ui.spec.ts |
| CRUD | application task service | REQUIREMENTS.md FR-01/02/06 | add, remove, retry, cancel | tests/test_service.py |
| Review Selection | review workspace issue list | REQUIREMENTS.md FR-15 | one active issue; pending/all filters; previous/next | review browser tests |
| Markdown Editing | CodeMirror 6 review editor | REQUIREMENTS.md FR-15 and DESIGN.md | one full document; live preview; Ctrl/Cmd+S | review browser tests |
| Review Save | application review service | TECHNICAL_DESIGN.md section 16 | atomic body save, version conflict, per-item state | tests/test_review.py |
| Unsaved Navigation | application HTML dialog | this contract | save/discard/continue for switch, return, close | review browser tests |

## Flow ledger

- Import: picker or native drop → validating → queued; invalid files are reported,
  valid siblings remain. Duplicates by resolved source path are skipped.
- Convert: validate immutable settings → queue → running → success/warning/failure.
  Running locks queue mutation/settings, but preserves preview and cancellation.
- Retry: failed/cancelled item becomes ready and gets a new execution record.
- Cancel current: immediate “正在取消” feedback, terminate worker, then continue queue.
- Cancel all: stop current and mark pending items cancelled. Published files remain.
- Close during conversion: app-owned dialog, focus initially on “继续转换”; confirmation
  cancels active work before closing. Escape returns without cancelling.
- Preview: source/render tabs, selected output only, stale responses ignored, local assets
  only. Missing files show a recoverable error. No generated example content.
- Settings: native selects preserve keyboard behavior; invalid range/output errors remain
  associated with their fields; begin is protected against duplicate invocation.
- Review: completed item or issue → full document loading → selected issue with linked
  original PDF page/region, source range and rendered block. All issues are accessible.
  Without reliable mapping, use page-level context and explain the missing precision.
- Review editing: one complete Markdown buffer → dirty indicator/live preview → atomic
  save → saved feedback. Chinese composition, history and shortcuts belong to CodeMirror.
  Async page/document replies cannot replace a newer selection or draft.
- Review marking: save current changes when needed, then mark the selected issue. All
  issues persisted as reviewed → “完成 · 已核对”; undo or editing a related block →
  “完成 · 需核对”. Other blocks keep their saved review state. Original warnings remain.
- Review recovery: write failure keeps draft and selected issue. If Markdown saved but
  review state failed, state this explicitly. Version conflict keeps draft and offers
  copy/reload; untraceable external edits clear reviewed flags on reload with explanation.
- Unsaved departure: switching tasks, leaving review or closing opens the shared dialog
  with save/discard/continue; Escape continues editing. Failed save does not navigate.
- Restore/open result: persist task index and saved review state. Open legacy results
  without inventing precise offsets. Missing PDF still permits editing; offer relinking.
  Changed PDF disables original-region location until the original source is restored.

## State and accessibility

Chinese labels and errors are owned by the app. System dialogs and select popup appearance
follow the OS. Buttons have semantic labels and focus-visible styles. Status has text and
polite announcements. Long names expose full values in the detail area. Loading and errors
keep panel geometry. Reviewed/pending state, PDF highlights and errors use text as well as
color. Keyboard users can reach issue navigation, page/zoom controls, save and review actions.
The narrow layout collapses the issue ledger and stacks documents without trapping scroll.
Task history is local and persistent; list removal never deletes files. No accounts, remote
persistence or URL query state is introduced. PDF page navigation is physical-page navigation,
not list pagination or printed document page labels.


## Startup and resource activity (0.2.1)

- Show the full shell first. The post-paint initialize operation is idempotent; settings and task mutations remain disabled with a reason until historical state is committed. Recovered settings load exactly once.
- Recognition prewarm runs after core restore. Users can submit conversions during prewarm; queued items say “等待识别资源” and start automatically. Cancelling a queued item preserves independent prewarm; a failed prewarm keeps the queue and exposes retry.
- One reserved header slot between brand and document actions owns resource activity. Current document/page/editor takes priority, with background recognition preparation still visible. Indeterminate work uses stage text and a spinner; no estimated percentages. Ready feedback clears briefly afterward while the recognition badge remains. Errors persist with the relevant retry.
- Switching or leaving a document invalidates old resource activity as well as old content replies. Failed module loading retries through a guarded reload that preserves the task and unsent settings. Existing dirty-editor departure continues to offer save/discard/continue.
- Status text truncates with the complete text available on hover. The reserved slot and narrow-window layout do not move controls as status changes.
