# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Ground truth for the list engine, computed WITHOUT the code under test.

A detector that asks the engine what the answer should be is the engine marking its own homework, and
that is precisely how five of the six severe findings in the 2026-07-31 audit stayed invisible: the tests
asserted that the mechanism RAN. Every answer here is reached another way:

    BY THE OTHER READER   `evaluate_filters` in memory, never `get_list` — so an assertion about which
                          records a filter returns is checked against a reader that shares no SQL with it
    BY ARITHMETIC         a bucket and its complement must account for the whole scoped table, which is
                          true of any partition and needs no knowledge of how the query was built
    BY THE DECLARATION    what an operator typed, read straight off the row

Nothing in this module calls `engine.ListRequest`, `engine.get_data` or `derived.predicate`. If it ever
does, the harness that uses it stops being evidence.
"""

import frappe
from frappe.utils.data import evaluate_filters

from tatva_connect.list_engine import derived


def value_of(field, row, snap):
	"""What a record READS AS, decided in memory off the declaration's own tuples.

	This is frappe's in-memory reader, which is the half of the promise that never touches SQL. The list
	is built by the other half, so comparing the two is a real cross-check rather than a restatement."""
	for bucket in field.buckets:
		terms = [[field.doctype, f[0], f[1], derived._substitute(f[2], snap)] for f in bucket.filters]
		if evaluate_filters(row, terms):
			return bucket.value
	return None


def reading_as(field, rows, snap):
	"""`{value -> {record names}}` for a set of already-fetched records. The DISPLAY half of the promise."""
	out = {}
	for row in rows:
		out.setdefault(value_of(field, row, snap), set()).add(row["name"])
	return out


def scoped_rows(doctype, field, scope):
	"""Every record in scope, with the columns the declaration reads — fetched by ONE plain query that
	names no bucket and no derived field, so it cannot inherit a defect in how buckets are composed."""
	return frappe.get_all(
		doctype, fields=["name", *field.depends_on], filters=scope, limit=0, order_by="name asc"
	)


def partitions(field, rows, snap):
	"""Whether the declaration claims every record in the set exactly once.

	`None` in the reading means a record no bucket claims — legal for a declaration that is not
	exhaustive, and the reason `is not set` exists — so this reports the fact rather than judging it."""
	reading = reading_as(field, rows, snap)
	return {
		"claimed": sum(len(names) for value, names in reading.items() if value is not None),
		"unclaimed": len(reading.get(None, set())),
		"total": len(rows),
	}


def declared_themes(dt, fieldname):
	"""The colours an operator typed, read straight off the stored row — never off the descriptor the
	server builds, which is the thing a surface-parity test is checking."""
	name = frappe.db.exists(derived.ROW_DOCTYPE, {"dt": dt, "fieldname": fieldname})
	if not name:
		return {}
	buckets = frappe.parse_json(frappe.db.get_value(derived.ROW_DOCTYPE, name, "buckets") or "[]") or []
	return {b.get("value"): b.get("theme") for b in buckets if isinstance(b, dict) and b.get("theme")}
