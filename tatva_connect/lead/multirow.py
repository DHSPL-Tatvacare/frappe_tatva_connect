"""The ONE 'latest row' rule for a multi-row lead section.

A multi-row section (lab, drug — `is_multi_row`) flattens to a SINGLE row in every consumer: the Data
tab, the headline sync, and a Smart View worklist. Before this module each consumer picked "latest" its
own way (three different tiebreaks), so two rows sharing one date resolved to a DIFFERENT row per surface
— a direct break of the CLAUDE.md invariant "one row ... in EVERY consumer".

The rule, expressed here for Python consumers and mirrored bit-for-bit in `smartview/api.py`'s SQL:
newest by the section's `row_key_field`, ties broken by `creation`, then by `name` — fully deterministic
and identical whether resolved in Python or by SQL `ROW_NUMBER() OVER (... ORDER BY row_key DESC,
creation DESC, name DESC)`. The rows modal is a different LAYER but not a different rule: it asks the
DB for the same order through `order_by()` below, and the flattened consumers read the head of
`sorted_child_rows`, so the table opens on the row the panel is already showing.
"""
from frappe.utils import cstr


def order_keys(row_key_field):
	"""THE ordering, declared ONCE as field names, newest-first on each: row key, then creation, then
	name. Every rendering below is built from this tuple, so a Python sorter and a DB `order_by` cannot
	drift — there is nothing to keep in step. A section with no row key falls to creation, then name."""
	return tuple(f for f in (cstr(row_key_field), "creation", "name") if f)


def order_by(row_key_field):
	"""The same ordering as an `order_by` clause, for a reader that asks the DB instead of sorting in
	Python. No backticks: frappe rejects them (`db_query` order-by validation) and quotes the field itself."""
	return ", ".join(f"{field} desc" for field in order_keys(row_key_field))


def _rank(row, row_key_field):
	"""THE ordering key for an in-memory sorter — the same fields `order_by` names, in the same order."""
	return tuple(cstr(row.get(field)) for field in order_keys(row_key_field))


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
