# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""WhatsApp Health joins Telephony Health, and every declared shortcut is finally placed on a page.

THE SECOND HEALTH REPORT. WhatsApp has the same shape as telephony and had the same blind spots: a
message that reached no lead, media a message is owed, and — the one no state count can show — an
inbound that matched no lead at all, which is DROPPED before a row exists (A.16). The drop leaves only
an Error Log line whose title is built from the event, so the card matches the stable tail rather than a
channel literal that would miss the day a second channel drops one. `Sends With No Status 7d` is the
outbound half: a send the provider never acknowledged is neither sent nor failed, and nothing counted it.

THE SHORTCUTS. A workspace renders from its `content` blocks. Observability declared eight shortcuts and
placed NONE of them, so `Integration Last Seen` and `Endpoint Breakdown` — the two reports its own prose
tells an operator to open — appeared on no page; Communications placed one of twelve, hiding Call Log,
WhatsApp Messages and every telephony setting behind a URL you had to know. `test_every_workspace_tile_
resolves` does not catch this: it checks that a PLACED shortcut resolves, never that a DECLARED one is
placed. Both files now place every shortcut they declare.

WHY A PATCH. `import_file.py:141` skips a standard JSON whose DB row is not older than the file, so on a
site whose desk was ever opened the bumped `modified` alone ships nothing. Goes through
`_desk.reimport_all`, never a hand-rolled import_file_by_path. No schema_setup twin: no doctype, no
field, no index.
"""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("tatva_connect", "workspace", "communications", "communications.json"),
		("observability", "workspace", "observability", "observability.json"),
	])
