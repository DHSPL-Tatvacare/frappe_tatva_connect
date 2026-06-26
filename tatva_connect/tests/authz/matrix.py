# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The visual-grid cell catalog — pure data assembly, no DB.

`cells()` returns the (principal × doctype × action) crossings worth showing in the report grid:
every concrete registry case, plus a small fixed principal×sensitive-doctype×action grid so the
grid always shows the audit-critical surface even when a vector has no case yet. Dedup keeps the
registry case's id when one exists. No Frappe, no DB — importable from a test or the tcsec CLI.
"""
from tatva_connect.tests.authz.registry import cases as _cases

# The doctypes a leak hurts most — the grid always shows these even with no curated case.
SENSITIVE_DOCTYPES = ["CRM Lead", "Contact", "CRM Call Log", "WhatsApp Message"]

# The principals whose access to the sensitive doctypes is always worth a visual row. Kept small
# and fixed: the horizontal-isolation grain user, the cross-app junk role, and the no-role baseline.
GRID_PRINCIPALS = ["grain_1", "junk_crossapp", "no_role"]

# The actions shown for the fixed grid (the read-side surface a grid communicates best).
GRID_ACTIONS = ["read", "field_read"]


def _key(principal, doctype, action):
	return (principal, doctype, action)


def cells():
	"""[{principal, doctype, action, id, expected}] — case-derived first, then the fixed grid.

	`id` is the registry case id when the cell came from a case, else "" (a grid-only cell).
	`expected` is the case verdict ("allow"/"deny") when known, else "" (unknown until run).
	"""
	out = []
	seen = set()

	for c in _cases.all_cases():
		k = _key(c.principal, c.doctype, c.action)
		if k in seen:
			continue
		seen.add(k)
		out.append({
			"principal": c.principal,
			"doctype": c.doctype,
			"action": c.action,
			"id": c.id,
			"expected": c.expected,
		})

	# Fixed principal × sensitive-doctype × action grid — fills the visual surface.
	for principal in GRID_PRINCIPALS:
		for doctype in SENSITIVE_DOCTYPES:
			for action in GRID_ACTIONS:
				k = _key(principal, doctype, action)
				if k in seen:
					continue
				seen.add(k)
				out.append({
					"principal": principal,
					"doctype": doctype,
					"action": action,
					"id": "",
					"expected": "",
				})

	return out


def principals():
	"""Distinct principals across all cells, in first-seen order (grid row order)."""
	ordered = []
	for cell in cells():
		if cell["principal"] not in ordered:
			ordered.append(cell["principal"])
	return ordered


def columns():
	"""Distinct (doctype, action) column keys across all cells, in first-seen order."""
	ordered = []
	for cell in cells():
		col = (cell["doctype"], cell["action"])
		if col not in ordered:
			ordered.append(col)
	return ordered
