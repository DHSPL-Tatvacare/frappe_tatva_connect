"""Every derived field this app declares. One file, so the answer to "what is derived?" has one place.

A declaration is data, not code: a fieldname, a label, and an ordered list of `value -> filter tuples`
read by both of frappe's evaluators. Nothing here decides anything; `derived.py` resolves it and
`engine.py` serves it. Adding a field on any doctype is an edit to this file and nothing else.

Every field here is proven by `tests/list_engine`, which iterates the registry and runs `verify()` — so
a declaration that cannot be filtered exactly as it is displayed fails the build the day it is written.
"""

from tatva_connect.list_engine.derived import NOW, TOMORROW_START, Bucket, DerivedField, register

TASK = "CRM Task"
CLOSED = ["Done", "Canceled"]

# Ranges are half-open (`>=` start, `< end`); an inclusive upper bound reads differently in SQL and Python.
DUE_STATE = register(
	DerivedField(
		doctype=TASK,
		fieldname="due_state",
		label="Task Status",
		order_by="due_date",
		buckets=[
			Bucket(
				"Overdue",
				[("status", "not in", CLOSED), ("due_date", "is", "set"), ("due_date", "<", NOW)],
			),
			Bucket(
				"Due Today",
				[
					("status", "not in", CLOSED),
					("due_date", ">=", NOW),
					("due_date", "<", TOMORROW_START),
				],
			),
			Bucket(
				"Upcoming",
				[("status", "not in", CLOSED), ("due_date", ">=", TOMORROW_START)],
			),
			Bucket(
				"No Due Date",
				[("status", "not in", CLOSED), ("due_date", "is", "not set")],
			),
			Bucket("History", [("status", "in", CLOSED)]),
		],
	)
)
