"""The ONE flattening rule for a multi-row lead section — and the ONE ordering both halves of it use.

A multi-row section (lab, drug — `is_multi_row`) flattens to a SINGLE reading in every consumer: the Data
tab, an activity form's prefill, a workflow criterion, and a Smart View worklist. Before this module each
consumer picked "latest" its own way (three different tiebreaks), so two rows sharing one date resolved to
a DIFFERENT row per surface — a direct break of the CLAUDE.md invariant "one row ... in EVERY consumer".

The ordering is declared once in `order_keys` and every rendering is built from it: newest by the
section's `row_key_field`, ties broken by `creation`, then by `name` — deterministic and identical whether
resolved by a Python sorter, by a DB `order_by`, or by a SQL window. The rows modal is a different LAYER
but not a different rule: it asks the DB for that same order, so the table opens on the reading the panel
is already showing.

TWO questions are asked of that order and they are NOT the same question:

  * `row_for_section` — the ADDRESS an edit lands on: one real row, the latest. A write needs a row.
  * `current_for_section` — what the section currently SAYS: per column, the value from the newest row
    that HAS one. A read needs a value.

They differ because a blank cell means "the form that wrote this row never asked", not "the answer is
empty" — rows on one section are written by different forms. Reading only the latest row therefore showed
a hole where the lead had an answer, while `detail.empty_everywhere` said the very same field was not
empty; `current_for_section` is that judgement applied to the value instead of only to the flag.
"""
import frappe
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


def is_blank(value):
	"""THE notion of blank the fallback below walks past — a cell that says NOTHING, never one that says zero.

	0 and False are answers and stay; None, an all-whitespace string and an empty set are silence. The panel's
	`detail.empty_everywhere` reads this same function, so the value a field shows and the flag that hides it
	can never disagree, and `smartview._blank_last` is its SQL spelling."""
	if value is None:
		return True
	if isinstance(value, str):
		return value.strip() == ""
	if isinstance(value, (list, tuple, dict)):
		return len(value) == 0
	return False


def _rows_of(doc, section):
	"""The child rows a section keeps on this doc — empty for a section that names no child table, which is
	the lead itself. The one place both questions below read the table from."""
	table = section.get("child_table_field")
	return (doc.get(table) if table else None) or []


def _cells(row):
	"""One row's stored cells. A child Document knows which of its attributes are real columns and which are
	framework internals; a plain mapping is already only cells.

	Asked by TYPE and never by `hasattr`: frappe's `_dict` answers every attribute with None rather than
	raising, so a duck-type test passes and then calls None."""
	from frappe.model.base_document import BaseDocument

	return row.get_valid_dict() if isinstance(row, BaseDocument) else dict(row)


def _has_ordering(section):
	"""Is there an ordering to flatten this section by? Deliberately NOT `detail._is_multi_row`, which asks a
	different question — whether the PANEL draws a table — and answers it about key-value sections too. A
	section that keeps many rows but declares no row key cannot be ordered, so it is read as a singleton."""
	return bool(section.get("is_multi_row") and section.get("row_key_field"))


def row_for_section(doc, section):
	"""THE one row an EDIT lands on, or None. Multi-row picks the latest by the rule above; single-row takes
	its one row. A real Document, because a caller here is about to write to it.

	The WRITE address, and only that: `_stage_section`, the panel's edit key and Upsert Child Row all ask
	this, so a value read from one cycle can never be written onto another. What a field currently READS is
	`current_for_section` — see the module docstring for why those are two questions."""
	rows = _rows_of(doc, section)
	if not rows:
		return None
	if _has_ordering(section):
		return latest_child_row(rows, section.get("row_key_field"))
	return rows[0]


def current_values(rows, row_key_field):
	"""THE reading of a multi-row section: per column, the value from the newest row that HAS one.

	Keys are the newest row's, so the shape is exactly what one row gives and a column blank in every row
	stays blank. Frappe's own columns (`name`, `creation`, `parent`, `idx`, `docstatus`) are never blank, so
	they never fall back and always describe the latest row."""
	ordered = sorted_child_rows(rows, row_key_field)
	if not ordered:
		return None
	current = frappe._dict(_cells(ordered[0]))
	for field in [f for f, value in current.items() if is_blank(value)]:
		for row in ordered[1:]:
			if not is_blank(row.get(field)):
				current[field] = row.get(field)
				break
	return current


def current_for_section(doc, section):
	"""THE reading every consumer displays, filters and prefills from, or None — `row_for_section`'s twin.

	A plain dict and never a Document, so a caller cannot write through what is a projection: the panel, an
	activity form's prefill, a workflow criterion and a Smart View column all take their value from here. A
	single-row section gives both twins the same answer."""
	rows = _rows_of(doc, section)
	if not rows:
		return None
	if _has_ordering(section):
		return current_values(rows, section.get("row_key_field"))
	return frappe._dict(_cells(rows[0]))
