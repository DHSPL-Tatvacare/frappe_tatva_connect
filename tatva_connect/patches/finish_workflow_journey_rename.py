"""W5.1, second half — the logs point at `journey`, and the desk says Journeys.

Declared end state: `CRM Workflow Step Log.journey` and `CRM Workflow Action Log.journey` carry the values
their `workflow_run` columns held, and the Automations workspace and its sidebar both label the list
Journeys and link to `CRM Workflow Journey`.

WHY [post_model_sync] AND NOT pre — the opposite of the doctype rename, and for a stated reason.
`frappe.model.utils.rename_field` says so in its own docstring: *"This functions assumes that doctype is
already synced"*. It does not ALTER a column; it copies (`update tabX set new = old`) and needs the NEW
column to exist. Sync is what creates it from the renamed field in the JSON. Run pre-sync it would find no
`journey` field, print "not found" and silently do nothing.

THE DOOR: `patches/_schema.rename_field`, never a bare `frappe.model.utils.rename_field` — a bare one is
in `tests/static/test_patch_ddl_lock._FORBIDDEN` and goes red. `_schema` exists because frappe caches a
table's column list and a schema change never invalidates it, so the next `has_column()` in the same
migrate reads a pre-change lie.

THE OLD COLUMN IS LEFT IN PLACE. `rename_field` copies rather than moves, and dropping a column is a
separate guarded step — the same call `retire_task_slot_columns` made for the slot columns.
Nothing reads it: the doctype JSON no longer declares it, so no code path can address it.

THE DESK GOES THROUGH `_desk.reimport` AND NOWHERE ELSE. Bumping `modified` alone ships nothing on a site
whose desk was ever opened (`import_file.py` skips a standard file that is not newer than its DB row), and
a hand-rolled `import_file_by_path(force=True)` DELETES and re-inserts the row — which on a developer_mode
site makes `Workspace.after_delete` rmtree the exported folder while the re-insert's export returns early,
so the patch eats its own JSON out of the app directory. `_desk.reimport` restores the exact bytes it read.
Both files go together because a Desk tile is a Link permitted only through a Workspace Sidebar of the
same name.

A fresh site baselines this line without running it: it is born with the field and the desk labels already
correct.
"""
import frappe

from tatva_connect.patches import _desk, _schema

LOGS = ("CRM Workflow Step Log", "CRM Workflow Action Log")
OLD, NEW = "workflow_run", "journey"

DESKS = (
	("tatva_connect", "workspace", "automations", "automations.json"),
	("workspace_sidebar", "automations.json"),
)


def execute():
	for doctype in LOGS:
		if frappe.db.has_column(doctype, OLD):
			_schema.rename_field(doctype, OLD, NEW)
	_desk.reimport_all(DESKS)
