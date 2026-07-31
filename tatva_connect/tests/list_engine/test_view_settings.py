# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Saving a kanban board grouped by a derived field, from all three entry points that resolve its columns.

`sync_default_columns` reads `frappe.get_meta(dt).get_field(column_field).fieldtype` with no `None`
guard, so a derived column field 500s the save — on `create`, on `create_or_update_standard_view` (the
live path) and on `fetch_and_update_kanban_columns`. Two properties are asserted here, and they are the
same two the whole layer is built on:

  * THE DECLARATION ANSWERS. A derived board stores the declared buckets, in declaration order, in the
    `{"name": value}` shape native builds for a Select — and a refresh merges them into the columns the
    rep already has without disturbing one of them.
  * NOTHING ELSE MOVES. A board grouped by a REAL Select, and every list / group_by view, comes back
    exactly as native returns it. Asserted by running both and comparing the stored document, not by
    reasoning about the code path.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.list_engine.test_view_settings
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.list_engine import derived, fields, views

TASK = "CRM Task"
PROBE = "ViewProbe"
FIELD = "due_state"
BUCKETS = list(derived.get(TASK, FIELD).options)

# Identity and the audit stamps differ between two runs of the same call; nothing else may.
_VOLATILE = {"name", "label", "creation", "modified", "modified_by", "owner", "idx", "docstatus"}


def _native(fn):
	import crm.fcrm.doctype.crm_view_settings.crm_view_settings as native

	return getattr(native, fn)


def _shape(doc):
	"""The stored view minus identity — what two callers of the same endpoint must agree on."""
	return {k: v for k, v in doc.as_dict().items() if k not in _VOLATILE and not k.startswith("_")}


class ViewSettingsCase(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.delete("CRM View Settings", {"dt": TASK, "user": "Administrator"})

	def _payload(self, **overrides):
		payload = {
			"doctype": TASK,
			"type": "kanban",
			"label": f"{PROBE} Board",
			"column_field": FIELD,
			"kanban_columns": "",
			"kanban_fields": "[]",
			"filters": {},
			"rows": "[]",
			"columns": "[]",
		}
		payload.update(overrides)
		return payload

	def _view_doc(self, **overrides):
		doc = frappe.get_doc(
			{
				"doctype": "CRM View Settings",
				"label": f"{PROBE} Stored",
				"dt": TASK,
				"user": "Administrator",
				"type": "kanban",
				"column_field": FIELD,
				"kanban_columns": "[]",
				"kanban_fields": "[]",
				**overrides,
			}
		)
		return doc.insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator

	# -- the declaration answers --------------------------------------------------------------------

	def test_standard_view_saves_the_declared_buckets_in_declaration_order(self):
		"""The live path: ViewControls clears kanban_columns on a column-field change and calls this."""
		doc = views.create_or_update_standard_view(self._payload())
		self.assertEqual(doc.column_field, FIELD)
		self.assertEqual(json.loads(doc.kanban_columns), [{"name": value} for value in BUCKETS])

	def test_create_saves_the_declared_buckets_too(self):
		doc = views.create(self._payload(label=f"{PROBE} Created"))
		self.assertEqual(json.loads(doc.kanban_columns), [{"name": value} for value in BUCKETS])

	def test_fetch_merges_the_buckets_and_preserves_every_existing_column(self):
		"""A column carries the rep's own state — `page_length` is Load More, `order` is a drag, `delete`
		hides it. Native only APPENDS what is missing; so does this."""
		kept = [
			{"name": "Overdue", "page_length": 40, "order": ["TASK-1", "TASK-2"]},
			{"name": "Retired Bucket", "delete": True},
		]
		doc = self._view_doc(kanban_columns=json.dumps(kept))

		columns = json.loads(views.fetch_and_update_kanban_columns(doc.name))

		self.assertEqual(columns[:2], kept)
		self.assertEqual(
			columns[2:], [{"name": value, "delete": True} for value in BUCKETS if value != "Overdue"]
		)

	def test_fetch_is_idempotent_for_a_derived_board(self):
		doc = self._view_doc(kanban_columns=json.dumps([{"name": value} for value in BUCKETS]))
		self.assertEqual(
			json.loads(views.fetch_and_update_kanban_columns(doc.name)), json.loads(doc.kanban_columns)
		)

	# -- nothing else moves -------------------------------------------------------------------------

	def test_a_real_select_board_is_saved_exactly_as_native_saves_it(self):
		payload = self._payload(column_field="status")

		native_doc = _shape(_native("create_or_update_standard_view")(dict(payload)))
		frappe.db.delete("CRM View Settings", {"dt": TASK, "user": "Administrator"})

		self.assertEqual(_shape(views.create_or_update_standard_view(dict(payload))), native_doc)

	def test_a_real_select_board_fetches_exactly_as_native_fetches_it(self):
		stored = json.dumps([{"name": "Backlog", "page_length": 40}])
		ours = self._view_doc(column_field="status", kanban_columns=stored)
		theirs = self._view_doc(column_field="status", kanban_columns=stored)

		self.assertEqual(
			json.loads(views.fetch_and_update_kanban_columns(ours.name)),
			json.loads(_native("fetch_and_update_kanban_columns")(theirs.name)),
		)

	def test_a_list_view_is_created_exactly_as_native_creates_it(self):
		payload = self._payload(type="list", column_field=None, kanban_columns="[]")

		ours = _shape(views.create(dict(payload, label=f"{PROBE} Ours")))
		theirs = _shape(_native("create")(dict(payload, label=f"{PROBE} Theirs")))

		self.assertEqual(ours, theirs)

	def test_a_group_by_view_is_created_exactly_as_native_creates_it(self):
		payload = self._payload(type="group_by", column_field=None, group_by_field=FIELD)

		ours = _shape(views.create(dict(payload, label=f"{PROBE} Ours")))
		theirs = _shape(_native("create")(dict(payload, label=f"{PROBE} Theirs")))

		self.assertEqual(ours, theirs)

	def test_fetch_on_a_non_kanban_view_returns_nothing_and_writes_nothing(self):
		doc = self._view_doc(type="list", column_field=FIELD, kanban_columns="[]")

		self.assertIsNone(views.fetch_and_update_kanban_columns(doc.name))
		self.assertEqual(frappe.db.get_value("CRM View Settings", doc.name, "kanban_columns"), "[]")
