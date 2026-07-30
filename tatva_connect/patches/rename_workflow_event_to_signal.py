"""W5.1, last leg — `CRM Workflow Event` becomes `CRM Workflow Signal`.

Declared end state: the inbox doctype, its table and every Link field pointing at it name the Signal.

WHY SIGNAL AND NOT EVENT. `event` already carries three meanings in this codebase — the Trigger's
`trigger_event`, frappe's own `doc_events`/`scheduler_events`, and a channel's outcomes — and a fourth
would defeat the point of W5. The CODE already said signal (`SIGNAL_DT`, `signals.py`, `awaiting_signal`);
the doctype was the only thing still saying Event, so this brings the name to the code rather than the
other way round.

WHAT DELIBERATELY KEEPS THE WORD `event`: the row's own `event_name` column and its `Event Name` label.
That field holds a channel OUTCOME (`task.completed`, `whatsapp.replied`) — the third legitimate meaning
above — so renaming it would import the ambiguity this leg exists to remove, not remove it.

Same door and same placement as the Journey rename, for the same reasons: `frappe.rename_doc("DocType", …)`
in `[pre_model_sync]`, because the folder is renamed in source and sync would otherwise create an empty
`CRM Workflow Signal` and strand the buffered inbox in `tabCRM Workflow Event`. Rejected: `_schema.ddl`
with a RENAME TABLE, which moves the table and leaves `tabDocType`, every DocField `options` and every
child `parenttype` naming a doctype that is gone.

NO FIELD RENAME AND NO INDEX RENAME. Nothing points at this doctype by a `workflow_event` fieldname — the
one inbound Link is `CRM Workflow Signal.consumed_by`, which points at the Journey and was already
repointed by the Journey rename. `SHOW INDEX` on the live table returns only column-named indexes
(`PRIMARY, creation, event_name, modified, status, subject_name`), all built by frappe from the JSON, and
`RENAME TABLE` carries them.

THE VISIBILITY SWITCH KEY NEEDS NO REKEY PATCH. `Workflow::CRM Workflow Event::visibility` becomes
`Workflow::CRM Workflow Signal::visibility` in `automation/registry.py` alone: `seed.sync_catalog()` is
authoritative — it prunes rows whose key left the registry and inserts the new one DORMANT. Verified on a
throwaway site rather than assumed, and verified too that the row has no `requires` and is named as a
parent by nothing, so the import-time `assert_valid_graph` cannot trip on it.

A fresh site baselines this line without running it and is born with the new name.
"""
import frappe

OLD, NEW = "CRM Workflow Event", "CRM Workflow Signal"


def execute():
	if not frappe.db.exists("DocType", OLD):
		return
	frappe.rename_doc("DocType", OLD, NEW, show_alert=False)
	frappe.reload_doctype(NEW)
