# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CRMMCPSettings(Document):
	"""Single. The numeric knobs for the MCP documentation server — the two rate counters that keep it
	from hammering the site, and the sizes of the answers it gives. On/off lives ONLY in the automation
	row `MCP::Docs::server`; this form carries no switch.

	Wired straight to the live server: `mcp/settings.config()` re-reads this row on every call, so a
	save takes effect on the very next one. A blank field falls back to the DEFAULTS declared there."""
