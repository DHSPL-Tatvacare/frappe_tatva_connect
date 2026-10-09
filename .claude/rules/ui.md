---
paths:
  - "tatva_connect/public/**"
  - "../frappe_tatva_crm/frontend/src/**"
---

# UI: before you write a line

The rules are the C.* list in `CLAUDE.md` and `docs/conventions/frontend/` (`crm-spa-ui-patterns.md`, `ui-rules/`). This file is the checklist that applies them.

1. Search before you build. Name what you searched in your answer.
   - frappe-ui and native crm components first: `ListView` family, `EmptyState`, `FormControl`, `Popover`, `Dialog`, `Filter`/`SortBy`/`ColumnSettings`, `useActiveTabManager`, `formatDate`.
   - Then ours in `frontend/src/tatva/`: `ResponsiveDialog`, `TatvaBottomSheet`, `fieldControl.js`, `GrainSelect`, `PredicateBuilder`, `smartView*`, `coalescedReload.js`, `notify.js`, `linkTitle.js`.
2. One `createResource` per component, bound to `resource.data`. Never copy into refs in `onSuccess`.
3. Layout with CSS, not JS. Theme tokens only, never a hex. Check both themes.
4. Mobile at about 390px on every change.
5. Business logic stays in the backend. The SPA is UI and thin dispatch.
6. Every upstream file you touch carries a `TATVA:` marker and a `CUSTOMIZATIONS.md` row.
7. Build with `yarn build` in the backend container, never `bench build`.
