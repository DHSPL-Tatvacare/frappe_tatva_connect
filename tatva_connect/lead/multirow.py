"""The ONE 'latest row' rule for a multi-row lead section.

A multi-row section (lab, drug — `is_multi_row`) flattens to a SINGLE row in every consumer: the Data
tab, the headline sync, and a Smart View worklist. Before this module each consumer picked "latest" its
own way (three different tiebreaks), so two rows sharing one date resolved to a DIFFERENT row per surface
— a direct break of the CLAUDE.md invariant "one row ... in EVERY consumer".

The rule, expressed here for Python consumers and mirrored bit-for-bit in `smartview/api.py`'s SQL:
newest by the section's `row_key_field`, ties broken by `creation`, then by `name` — fully deterministic
and identical whether resolved in Python or by SQL `ROW_NUMBER() OVER (... ORDER BY row_key DESC,
creation DESC, name DESC)`. The UI "More" modal is a different LAYER but not a different rule: it reads
`sorted_child_rows` and the flattened consumers read its head, so history opens on the row the panel is
already showing.
"""
from frappe.utils import cstr


def _rank(row, row_key_field):
	"""THE ordering key, written once: row_key, then creation, then name — the same three the smartview
	ROW_NUMBER orders by, so a Python reader and the SQL never disagree about which row is newer."""
	return (cstr(row.get(row_key_field)), cstr(row.get("creation")), cstr(row.get("name")))


def sorted_child_rows(rows, row_key_field):
	"""Every row of a multi-row child table, newest first, under the ONE ordering above.

	The history modal reads this whole list and the flattened consumers read its head, so a section's
	'latest' and the top of its history are the same row by construction — there is no second ordering
	to drift from. Stable, so rows equal on all three keys keep their table order."""
	return sorted(rows or [], key=lambda r: _rank(r, row_key_field), reverse=True)


def latest_child_row(rows, row_key_field):
	"""The single latest row from a multi-row child table, or None — the head of `sorted_child_rows`."""
	ordered = sorted_child_rows(rows, row_key_field)
	return ordered[0] if ordered else None
