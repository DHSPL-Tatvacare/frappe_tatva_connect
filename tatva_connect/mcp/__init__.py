# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The MCP documentation server — a read-only door onto the handbook, the live schema and the Desk map.

Plans: `docs/plans/search/2026-09-04-mcp-docs-server/`, `docs/plans/search/2026-10-19-mcp-masters-and-relations/`.

The model, in one paragraph: an AI agent belonging to a member of staff speaks MCP over HTTP to ONE
endpoint on this site, signed in as that person (OAuth or their own API key), and asks for documentation
and structure, and for the rows of only the setup tables an operator lists under Access. Every read
runs on the read replica, under that person's own permissions, and nothing in this package writes.

Three rules hold across every module here, and `tests/mcp/test_readonly_static.py` enforces them:
  * no write call of any kind — no save/insert/db.set_value, and no frappe.log_error (a master write
    inside a replica-switched request); diagnostics go to frappe.logger("mcp"), which is a file.
  * rows come only from the doctypes listed in Tatva Platform Settings → Access → Data Access for AI Agents → Readable Tables, and only
    through `get_list` / `get_doc` with Frappe's permission checks; no lock is ever asked for.
  * no caller input ever reaches SQL as an identifier.
"""


import re

_WORD = re.compile(r"[a-z0-9]+")


def words(text):
	"""The search words in some text: lower case, and `-`, `_` and spaces all split, so `Inside Sales` finds `Inside-Sales`."""
	return [word for word in _WORD.findall((text or "").lower()) if len(word) > 1]


class ToolError(Exception):
	"""A tool refusing its arguments or its subject, in words the agent can act on.

	Lives on the package so a tool module and the catalog can both reach it without importing each
	other. `server.py` turns it into a tool-level error result, which is what the protocol asks for:
	a tool that refuses is a RESULT, not a transport fault.
	"""
