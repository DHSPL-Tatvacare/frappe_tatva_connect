# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Test-only activity scaffolding: a grain and a task type carrying a real schema — minted, then destroyed.

A task type, its grain and the fields it declares are OPERATOR data. A test that reads them off a dev
site asserts the seed, not the code: reseed, and it goes red for a reason that is no defect. So a test
that needs an activity type mints its own — a grain no operator would name — and tears the lot down.

Shared the way `tests/api/partner_fixture.py` already is: the fixture lives in the package that owns
the concern, and its consumers import it from wherever they live. `schema` is passed through verbatim
so a caller declares its own `target`s — the routing between a promoted column and the JSON payload is
the thing under test, and a fixture that could not express it would prove nothing.
"""
import frappe

# Distinctive enough that no operator taxonomy can collide with them, so teardown is unambiguous.
VERTICAL = "ZZ One Brain Line"
GROUP = "ZZ One Brain Group"

# (doctype, name) for what THIS module actually created, torn down in reverse. A row that already
# existed is left alone: the fixture may not delete something it did not mint.
_MADE = []


def _make(doctype, name, values):
	if frappe.db.exists(doctype, name):
		return name
	frappe.get_doc({"doctype": doctype, **values}).insert(ignore_permissions=True)
	_MADE.append((doctype, name))
	return name


def key_for(type_name, vertical=VERTICAL, group=GROUP, program=""):
	"""The composite PK a type will autoname to. Restates `CRM Task Type`'s own `format:` autoname —
	the one place a test may, because it must know the key before the record exists to stay idempotent."""
	return f"{vertical}::{group}::{program}::{type_name}"


def mint_type(type_name, schema, vertical=VERTICAL, group=GROUP, program="", rules=(), extra=None):
	"""A CRM Task Type on a minted grain, plus the masters its axes Link to. Committed: the composer
	reads it live, on the far side of the per-test rollback.

	`rules` is passed through verbatim beside `schema` for the same reason schema is: a type's reactions are
	operator data, and a fixture that could not declare them could not test the compile.

	`extra` is any of the type's OWN columns — `visit_mode`, the three location-condition columns,
	`is_logged_complete`. Passed through the same way and for the same reason: enforcement is declared on the
	type, so a fixture that could not declare it could not drive the gate that reads it. Additive; every
	existing caller is untouched."""
	_make("CRM Vertical", vertical, {"vertical_name": vertical})
	_make("CRM Group", group, {"group_name": group})
	if program:
		_make("CRM Program", program, {"program_name": program})
	name = _make("CRM Task Type", key_for(type_name, vertical, group, program), {
		"type_name": type_name, "vertical": vertical, "group": group, "program": program,
		"schema": [dict(f) for f in schema],
		"rules": [dict(r) for r in rules],
		**(extra or {}),
	})
	frappe.db.commit()
	return name


def track(doctype, name):
	"""Adopt a record the consumer brought into being — a rename mints a key this module never inserted,
	and an untracked key is exactly the orphan these tests exist to forbid."""
	_MADE.append((doctype, name))


def teardown():
	"""Drop everything minted, newest first — a type before the grain masters it Links to."""
	for doctype, name in reversed(_MADE):
		if frappe.db.exists(doctype, name):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
	_MADE.clear()
	frappe.db.commit()
