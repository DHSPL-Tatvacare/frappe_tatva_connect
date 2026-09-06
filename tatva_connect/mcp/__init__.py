# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The MCP documentation server — a read-only door onto the handbook, the live schema and the Desk map.

Design: vault `Projects/frappe-crm/05-roadmap/08-mcp-docs-server.md`. Build order:
`docs/plans/2026-09-04-mcp-docs-server.md`.

The model, in one paragraph: an AI agent belonging to a member of staff speaks MCP over HTTP to ONE
endpoint on this site, authenticated by that person's own Frappe API key, and asks for documentation
and structure — never records. Every read runs on the read replica, under that person's own
permissions, and nothing in this package writes.

Three rules hold across every module here, and `tests/mcp/test_readonly_static.py` enforces them:
  * no write call of any kind — no save/insert/db.set_value, and no frappe.log_error (a master write
    inside a replica-switched request); diagnostics go to frappe.logger("mcp"), which is a file.
  * no tool returns a row of any doctype. The ABSENCE of that capability is the guarantee; a filter
    can be wrong, a missing tool cannot.
  * no caller input ever reaches SQL as an identifier.
"""


class ToolError(Exception):
	"""A tool refusing its arguments or its subject, in words the agent can act on.

	Lives on the package so a tool module and the catalog can both reach it without importing each
	other. `server.py` turns it into a tool-level error result, which is what the protocol asks for:
	a tool that refuses is a RESULT, not a transport fault.
	"""
