# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Force the Communications workspace + sidebar to pick up the AI Voice section.

A bumped `modified` is not enough on a site whose desk was ever touched: `import_file.py` skips a
standard file whose DB copy looks newer, so the AI Voice setup section and the CRM AI Voice Account
sidebar item would migrate silently green and appear nowhere. Re-import both, force. Third time this
has been needed — the rule is in CLAUDE.md.
"""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("tatva_connect", "workspace", "communications", "communications.json"),
		("workspace_sidebar", "communications.json"),
	])
