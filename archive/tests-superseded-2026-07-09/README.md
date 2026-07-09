# Superseded authz tests — archived 2026-07-09

Dead code kept per constitution A.14 (archived, never silently deleted). These files were folded
into the rebuilt authz framework at `tatva_connect/tests/authz/`. The archive is not on the test
path (it sits outside `tatva_connect/`), so nothing here runs.

| Archived file | Replaced by |
|---|---|
| `notes__test_note_scope.py` | `authz/test_registry_cases.py::test_A9_child_scope_inheritance` — cases `A9-note-*` (switch-on peer-blocked, switch-off stock exposure, inheritance shape) |
| `tasks__test_task_scope.py` | same handler — cases `A9-task-*` plus `A9-task-assignee-sees-own-on-hidden-parent` (least-privilege carve-out) |
| `telephony__test_callog_scope.py` | same handler — cases `A9-callog-*` plus `A9-callog-links-only-fail-closed` (orphan fail-closed) |
| `whatsapp__test_whatsapp_message_scope.py` | same handler — cases `A9-whatsapp-*` (keys on `reference_name`, not `reference_docname`) |
| `access__test_visibility_parity.py` | (archived earlier this session; not part of the 2026-07-09 A9/A10 consolidation) |

## What moved (per child doctype: CRM Task / CRM Call Log / FCRM Note / WhatsApp Message)

- **switch-ON scope**: the out-of-scope peer (grain_2) cannot read a child on a grain_1 lead it
  cannot see, while the in-scope owner (grain_1) can — asserted through both
  `visibility.scoped_has_permission` and the wired `frappe.has_permission` hook.
- **switch-OFF stock exposure**: with the visibility switch OFF, `scoped_pqc` is empty and the peer
  CAN read the child (stock CRM) — the documented delta.
- **inheritance shape**: the list PQC inherits parent scope via `owner` + `reference_doctype` +
  the right ref column (`reference_docname`, or `reference_name` for WhatsApp Message), with an
  `assigned_to` clause only on CRM Task.
- **carve-outs**: a links-only Call Log (no reference parent) fails closed even in-scope; a task
  assignee sees their own task on a hidden parent.

All 14 `A9-*` cases live in `authz/registry/cases.py` and run in
`authz/test_registry_cases.py::test_A9_child_scope_inheritance`.

## NOT archived: `tatva_connect/tests/security/test_vapt_authz.py` (KEPT)

Its **L3 method-gate** layer — the only runtime exercise of `access/native_guards.py` — was
consolidated into vector **A10** (`test_A10_native_method_bypass`, 9 `A10-*` cases: peer denied on a
row it cannot read, no-role denied at the doctype matrix, authorized owner not denied). But the file
retains coverage with no home in the registry runner, so it is kept (never lose coverage):

- **Comment IDOR by a PEER** (a Sales User editing another Sales User's Comment via load+save,
  `if_owner` scope) — the endpoint sweep runs only the 4 hostile personas, not a peer.
- **File private-blob BAC** (a no-role user forging a File row that references a victim's private
  blob via a crafted `file_url`) — the endpoint sweep inserts only an empty File.
- **Helpdesk `get_article_stats` gate** (`helpdesk.api.article.get_article_stats`) — the endpoint
  sweep covers the LMS article-stats method, not the Helpdesk one.
- **LMS `_force_published` narrowing** (white-box unit: a non-privileged caller's `{published:0}` is
  pinned to `{published:1}`).
- **Positive no-regression guards** (Sales User reads Contact, Manager deletes Contact, Agent reads
  HD Ticket, owner edits own Comment) — the metamorphic framework asserts `actual ⊆ native` (deny
  only), so it does not assert positive access is preserved.

Its L1 doctype matrix and L4 drift guard ARE superseded (by `test_catastrophe_sweep.py` /
`test_endpoint_sweep.py` + `vapt/findings.py`, and by `test_surface_audit.py` respectively).
