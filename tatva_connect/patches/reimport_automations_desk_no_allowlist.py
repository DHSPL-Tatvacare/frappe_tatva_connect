# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The Field Allowlist leaves the Automations desk, because there is no longer such a thing.

Declared end state: the Automations workspace's Setup card and its sidebar offer Workflow and Contact
Limit and nothing else, and the Setup prose has three numbered steps beginning at Action Groups.

WHAT CHANGED UNDERNEATH. `can_read` / `can_set` are gone. What a workflow may read and write is the grain
contract, which already answered that question — the ticks were a second, weaker allowlist on top of it,
so a field could be entitled to a grain and still unreachable for no reason an operator could see. Two
Desk pages that existed to tick those boxes now tick nothing, and the prose walked an operator through
them as step one of four.

The doctypes themselves stay and are not touched here: `CRM Lead API Field` is the partner API's field
catalog and `CRM Task Type Field` is the activity form schema. Only the two Links that framed them as an
allowlist go, and only from this desk.

WHY A PATCH AT ALL. `import_file.py:141` skips a standard JSON whose DB row is not older than the file, so
on any site whose desk was ever opened the edit migrates silently green and changes nothing. The bumped
`modified` on both files is necessary and not sufficient.

Both files, because a Desk tile is a Link permitted only through a Workspace Sidebar of the same name.

Through `patches/_desk.reimport`, never a hand-rolled `import_file_by_path(force=True)`: force DELETES and
re-inserts, and on a developer_mode site that rmtree's the exported folder while the re-insert's export
returns early, so the patch eats its own JSON.

A fresh site baselines this line without running it: it is born with both files already imported.
"""
from tatva_connect.patches import _desk

DESKS = (
	("tatva_connect", "workspace", "automations", "automations.json"),
	("workspace_sidebar", "automations.json"),
)


def execute():
	_desk.reimport_all(DESKS)
