# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The Infrastructure desk gains the documentation server: a guided setup section and an MCP Settings shortcut.

Declared end state: Infrastructure carries a "Documentation server (MCP)" section between its prose and
its shortcut strip — what the server is, then four numbered steps (switch it on, set the limits, issue a
key, hand over the address) — and a shortcut opening `CRM MCP Settings`.

WHY THE STEPS SAY WHAT THEY SAY. The server ships dormant behind `MCP::Docs::server`, so an operator who
finds the Settings form first would set limits on something that answers "not enabled" — the switch is
step one for that reason. And staff do NOT generate their own keys on this site: access on the User
doctype is deliberately narrowed, so the key is issued by an administrator and handed over once, which
is the step an operator would otherwise have no way to guess.

WHY A PATCH AT ALL. `import_file.py:141` skips a standard JSON whose DB row is not older than the file,
so on any site whose desk was ever opened the edit migrates silently green and changes nothing. The
bumped `modified` is necessary and not sufficient. Goes through `_desk.reimport_all`, never a
hand-rolled import_file_by_path.

No sidebar twin: this adds a shortcut inside the workspace body, not a Desk tile, and a tile is the only
thing that needs a Workspace Sidebar entry of the same name.
"""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("tatva_connect", "workspace", "infrastructure", "infrastructure.json"),
	])
