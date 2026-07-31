# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""TATVA override of the three `CRM View Settings` savers that resolve a kanban board's columns.

`sync_default_columns` (`crm_view_settings.py:179-197`) asks `frappe.get_meta` for the board's column
field and reads `.fieldtype` off the answer with no `None` guard. A derived field is not in meta — that
is what it is — so saving a board grouped by one raises `AttributeError: 'NoneType' object has no
attribute 'fieldtype'`. Three whitelisted methods reach that line: `create` (`:61`),
`create_or_update_standard_view` (`:232`, the live path a rep hits on every column-field change) and
`fetch_and_update_kanban_columns` (`:296`).

Two of the three ask the helper only `if not kanban_columns`, so they need no reimplementation: the
board's columns are PRE-FILLED from the declaration and native, finding them already there, never calls
the helper at all. The third calls it unconditionally, so that one merge is mirrored here — the same
existing-column merge and the same `{"name": ..., "delete": True}` marking native does at `:299-301`.

THE LINE. A view whose `column_field` is a real column — and every list and group_by view, whatever it
is grouped by — reaches native with the CALLER'S OWN arguments and returns native's own answer. The
only thing this module supplies is the one answer `frappe.get_meta` cannot give.

The bucket values are never restated here; they are read from `derived`, in declaration order, in the
`{"name": value}` shape native builds for a Select.

Wired at `hooks.py` `override_whitelisted_methods`, the same seam as `api/task_lenses.py`; the fork
stays dispatch-only. `update` (`:88-119`) needs nothing — it never calls the helper.

Plan: docs/plans/tasks-ui/2026-07-30-derived-fields-list-engine.md §16 item 3.
"""

import json

import frappe
from frappe.utils import parse_json

from tatva_connect.list_engine import derived


def _board_field(doctype, view_type, column_field):
	"""The derived field a saved board is grouped by — None for a real column, and for every view that
	is not a kanban, which is every case native already answers for itself."""
	if view_type != "kanban" or not (doctype and column_field):
		return None
	return derived.get(doctype, column_field)


def _with_board_columns(view):
	"""The caller's payload, returned untouched unless it is a derived board still missing its columns.

	Native asks the helper on EITHER of two branches (`:59-62`, `:231-234`): `kanban_columns` when the
	board has none, and `columns` when THAT is empty — a kanban view reaches the second branch too, and
	the helper's own kanban arm fires there just the same. So both keys are filled, with the one list
	native would have assigned for a real Select, and the helper is never called on any branch."""
	given = frappe._dict(view)
	field = _board_field(given.dt or given.doctype, given.type, given.column_field)
	if not field:
		return view
	filled = dict(given)
	columns = [{"name": value} for value in field.options]
	for key in ("kanban_columns", "columns"):
		if not parse_json(given.get(key) or "[]"):
			filled[key] = columns
	return filled


@frappe.whitelist()
def create(view: dict):
	from crm.fcrm.doctype.crm_view_settings.crm_view_settings import create as _native

	return _native(_with_board_columns(view))


@frappe.whitelist()
def create_or_update_standard_view(view: dict):
	from crm.fcrm.doctype.crm_view_settings.crm_view_settings import (
		create_or_update_standard_view as _native,
	)

	return _native(_with_board_columns(view))


@frappe.whitelist()
def fetch_and_update_kanban_columns(name: str | int):
	"""The one caller that asks the helper unconditionally, so the merge is mirrored rather than
	pre-empted. Native's loop, native's marking, native's save — only the column list comes from us."""
	from crm.fcrm.doctype.crm_view_settings.crm_view_settings import (
		fetch_and_update_kanban_columns as _native,
	)

	doc = frappe.get_doc("CRM View Settings", name)
	field = _board_field(doc.dt, doc.type, doc.column_field)
	if not field:
		return _native(name)

	existing_columns = parse_json(doc.kanban_columns or "[]")
	existing_column_names = [column.get("name") for column in existing_columns]
	for value in field.options:
		if value not in existing_column_names:
			existing_columns.append({"name": value, "delete": True})

	doc.kanban_columns = json.dumps(existing_columns)
	# No bypass: `CRM View Settings` grants write to Sales User, Sales Manager and System Manager, so the
	# permission engine can answer this itself. Native passes `ignore_permissions=True` here; ours does not
	# need to, and a hole that is not opened needs no review marker.
	doc.save()
	return doc.kanban_columns
