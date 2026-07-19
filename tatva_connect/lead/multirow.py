"""The ONE 'latest row' rule for a multi-row lead section.

A multi-row section (lab, drug — `is_multi_row`) flattens to a SINGLE row in every consumer: the Data
tab, the headline sync, and a Smart View worklist. Before this module each consumer picked "latest" its
own way (three different tiebreaks), so two rows sharing one date resolved to a DIFFERENT row per surface
— a direct break of the CLAUDE.md invariant "one row ... in EVERY consumer".

The rule, expressed here for Python consumers and mirrored bit-for-bit in `smartview/api.py`'s SQL:
newest by the section's `row_key_field`, ties broken by `creation`, then by `name` — fully deterministic
and identical whether resolved in Python or by SQL `ROW_NUMBER() OVER (... ORDER BY row_key DESC,
creation DESC, name DESC)`. Full history stays a different layer (the UI "More" modal); this is only the
one flattened row.
"""
from frappe.utils import cstr


def latest_child_row(rows, row_key_field):
	"""The single latest row from a multi-row child table, or None.

	`rows`: the child docs; `row_key_field`: the section's date/sequence key. `max` by
	(row_key_field, creation, name) — a tie on the key falls to creation, then name, the SAME order
	smartview's ROW_NUMBER uses, so every consumer agrees on which row is 'latest'."""
	if not rows:
		return None
	return max(rows, key=lambda r: (cstr(r.get(row_key_field)), cstr(r.get("creation")), cstr(r.get("name"))))
