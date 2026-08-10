"""Activity Select values become rows in `CRM Task Option`, the way lead stages are rows in `CRM Lead Stage`.

THE DEFECT. A Select `CRM Task Type Field` carries its values as a newline blob in `options`, per task
type. `lsq_status` is declared on 59 task types with 11 different vocabularies, and
`describe.activity_schema_fields` collapses declarations by fieldname — `order_by parent, idx`,
first-definition-wins — so it answers with whichever task type sorts first. On this bench that is
`Goodflip-Care::Anaya::::Chemo Readiness`, so every workflow picker was handed `Active/Inactive` and an
unrelated Anaya activity supplied both the LABEL and the OPTIONS for every other type. A seeded
condition on `Connected - Completed` had no matching option and drew an empty select.

WHY ROWS. A blob has no identity, so two blobs cannot be merged without losing which task type each
value came from. Rows do — `CRM Lead Stage` holds 332 stages across every program under one field
because each row names its program. `CRM Task Option.task_type` is the same scoping, and it carries
vertical/group/program with it.

WHAT THIS DOES NOT DO, deliberately: it does not touch stored answers. `CRM Task Answer.value` keeps
the bare string it holds today (`Connected - Completed`), so Smart Views, the Data Tab and every report
reading those values are unaffected. Only where the OPTION LIST comes from changes. Re-keying 1,771
answers to a row id would have bought nothing and broken every reader.

Blobs are left in place too — this patch only ADDS the rows. Reading options from the rows instead of
the blob is a code change in `describe`/`compiled_fields`; until that lands the rows are inert and
nothing behaves differently. That ordering is on purpose: data first, switch second, no window where
the picker has neither.

DRY RUN BY DEFAULT. `execute()` reports and writes nothing. Pass apply=True to commit.
IDEMPOTENT. A row is matched on (task_type, fieldname, option_value), not on its hash name.
"""

import frappe

MASTER = "CRM Task Option"


def declarations():
	"""Every Select declaration on a task type, values split out of the blob, order preserved."""
	out = []
	for r in frappe.get_all(
		"CRM Task Type Field",
		filters={"parenttype": "CRM Task Type", "fieldtype": "Select"},
		fields=["name", "parent", "fieldname", "options"],
		order_by="parent, idx",
	):
		values = [v.strip() for v in (r.options or "").split("\n") if v.strip()]
		if values:
			out.append((r, values))
	return out


def execute(apply=False):
	wanted, seen = [], set()
	for r, values in declarations():
		for position, value in enumerate(values):
			key = (r.parent, r.fieldname, value)
			if key in seen:
				continue
			seen.add(key)
			wanted.append((r.parent, r.fieldname, value, position))

	existing = {
		(o.task_type, o.fieldname, o.option_value)
		for o in frappe.get_all(MASTER, fields=["task_type", "fieldname", "option_value"], limit_page_length=0)
	}
	missing = [w for w in wanted if (w[0], w[1], w[2]) not in existing]

	print(f"declarations carrying values : {len(declarations())}")
	print(f"distinct option rows wanted   : {len(wanted)}")
	print(f"already present               : {len(wanted) - len(missing)}")
	print(f"to create                     : {len(missing)}")
	if not apply:
		print("\nDRY RUN — nothing written. Re-run with apply=True to commit.")
		return

	for task_type, fieldname, value, position in missing:
		frappe.get_doc({
			"doctype": MASTER, "task_type": task_type, "fieldname": fieldname,
			"option_value": value, "display_label": value, "selectable": 1, "position": position,
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — patch, operator-owned config
	frappe.db.commit()
	print(f"\nAPPLIED — {len(missing)} option rows created, {frappe.db.count(MASTER)} total.")
