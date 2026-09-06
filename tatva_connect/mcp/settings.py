# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The numeric config — read fresh on every call, exactly as the partner API reads its own.

A Single is one cheap row read, so there is no cache and no cache-clear hook: an operator saves the
form and the very next call obeys it. A blank field falls back to the DEFAULTS below, and a read that
fails at all falls back to them too — a settings form that cannot be read must never be the reason
documentation stops answering.

`0` means unlimited for the two rate counters and only for those. On a size, `0` would mean an answer
with nothing in it, so a size of 0 falls back to its default rather than becoming a footgun.
"""
import frappe

SETTINGS = "CRM MCP Settings"

DEFAULTS = {
	"window_seconds": 60,
	"per_user_rate": 120,
	"global_rate": 600,
	"search_max_hits": 8,
	"schema_max_hits": 15,
	"page_characters": 12000,
}

UNLIMITED_WHEN_ZERO = frozenset({"per_user_rate", "global_rate"})


def config():
	"""The knobs, as integers, with every blank and every unusable zero already resolved."""
	try:
		row = frappe.db.get_singles_dict(SETTINGS) or {}
	except Exception:
		frappe.logger("mcp").error("settings read failed; using defaults", exc_info=True)
		return dict(DEFAULTS)

	resolved = {}
	for field, default in DEFAULTS.items():
		value = row.get(field)
		value = default if value in (None, "") else int(value)
		if value == 0 and field not in UNLIMITED_WHEN_ZERO:
			value = default
		resolved[field] = value
	return resolved
