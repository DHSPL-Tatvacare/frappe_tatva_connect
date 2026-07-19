# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""R3 — the OUTPUT leg is ONE declaration, not two hand-typed lists.

Files, Notes and Calls each hand-listed their read shape TWICE: once in `_xxx_view` (the single-record
read) and once in the `*_list` query's column select. Nothing forced the two to agree — a maintainer who
renamed or added an output field in one had no reason to touch the other. `_VIEW_FIELDS` closes that:
`_xxx_view` and `*_list` now both read the SAME module-level structure, so a `file_list`/`_file_view`
pair (and the note/call equivalents) cannot drift apart the way the hand-typed pair used to.

These tests hit the real bench — a real lead, a real file/note/call — never a mock, per this repo's
testing rule: assert outcomes, not calls. The caller is a trusted System Manager (no mapping), the same
posture `test_partner_contract.py` takes: these tests pin the OUTPUT contract, not the grain gate (which
has its own coverage under tests/authz).

GAP 8 (notes): `_create_one` used to read `data.get('content'/'external_id'/'created_at')` raw and never
called `collect(NOTE_FIELDS, data)`, unlike `_update_one`. The two tests in
`TestNoteCreateRoutesThroughCollect` pin that create now goes through the SAME `collect` call update does.
"""
import unittest
from unittest.mock import patch

import frappe

from tatva_connect.api import partner, partner_call, partner_file, partner_note
from tatva_connect.api._base import ACTION_CREATED

VERTICAL, GROUP = "GoodFlip Care", "Anaya"


class _TrustedCallerCase(unittest.TestCase):
	"""Trusted System Manager (no mapping) — mirrors test_partner_contract.py's posture."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.mp, cls.is_sysmgr = None, True

	def setUp(self):
		self.sp = f"output_leg_{frappe.generate_hash(length=6)}"
		frappe.db.savepoint(self.sp)
		self._form = frappe.form_dict
		frappe.local.response = frappe._dict()

	def tearDown(self):
		frappe.form_dict = self._form
		try:
			frappe.db.rollback(save_point=self.sp)
		except Exception:
			frappe.db.rollback()

	def _lead(self, phone):
		frappe.form_dict = frappe._dict({
			"mobile_no": phone, "first_name": "Output Leg Test",
			"custom_vertical": VERTICAL, "custom_group": GROUP,
		})
		_u, mp, s, pf, ca = partner._caller_fields()
		doc, _action = partner._upsert_one(frappe.form_dict, mp, s, pf, ca, [])
		return doc.name

	def _hit(self, fn, **args):
		"""Drive one @_api endpoint directly (no HTTP), the way _drive() in the OpenAPI lock does."""
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict(args)
		fn()
		return dict(frappe.local.response)


# -- structural: the select list and the view are the SAME structure, by construction --------------

class TestViewFieldsDriveTheListSelect(unittest.TestCase):
	"""_VIEW_FIELDS is the ONE structure; _LIST_COLUMNS is derived from it, not hand-typed again. If a
	future edit reintroduces a second, separately-typed column list, this is where it is caught."""

	def test_file_list_columns_are_exactly_the_view_fields_columns(self):
		declared = {c for _key, cols, _resolve in partner_file._VIEW_FIELDS for c in cols}
		self.assertEqual(set(partner_file._LIST_COLUMNS), declared)

	def test_call_list_columns_are_exactly_the_view_fields_columns(self):
		declared = {c for _key, cols, _resolve in partner_call._VIEW_FIELDS for c in cols}
		self.assertEqual(set(partner_call._LIST_COLUMNS), declared)

	def test_note_list_columns_are_exactly_the_view_fields_columns(self):
		declared = {c for _key, cols, _resolve in partner_note._VIEW_FIELDS for c in cols}
		self.assertEqual(set(partner_note._LIST_COLUMNS), declared)

	def test_file_type_is_read_through_the_input_contract_not_a_fresh_literal(self):
		"""GAP 6/9: custom_file_type used to be a literal in three read paths. FILE_TYPE_FIELD is
		sourced from FILE_FIELDS (the input contract) and _VIEW_FIELDS/file_list both read THAT."""
		target = next(s.target for s in partner_file.FILE_FIELDS if s.fieldname == "file_type")
		self.assertEqual(partner_file.FILE_TYPE_FIELD, target)
		file_type_cols = dict(
			(key, cols) for key, cols, _resolve in partner_file._VIEW_FIELDS
		)["file_type"]
		self.assertEqual(file_type_cols, (partner_file.FILE_TYPE_FIELD,))


# -- behavioural: list and get must be pixel-identical for the same record --------------------------

class TestFileOutputLeg(_TrustedCallerCase):
	def test_get_and_list_agree_on_shape_and_values(self):
		lead = self._lead("+919812390001")
		view = self._hit(
			partner_file.file_attach, lead=lead, filename="leg-test.txt",
			file_type="Prescription", content_base64="cHJvYmU=",
		)["data"]

		got = self._hit(partner_file.file_get, name=view["name"])["data"]
		listed = self._hit(partner_file.file_list, lead=lead, limit=10)["data"]["files"]

		self.assertEqual(len(listed), 1)
		self.assertEqual(got, listed[0], "file_get and file_list disagree on the SAME record")
		self.assertEqual(got, view, "the create response and a fresh read disagree")

	def test_view_shape_is_unchanged(self):
		"""Pins the exact key set _file_view returns — the refactor to one declaration must not change
		the JSON shape a partner already depends on."""
		lead = self._lead("+919812390002")
		view = self._hit(
			partner_file.file_attach, lead=lead, filename="shape-test.txt", content_base64="cHJvYmU=",
		)["data"]
		self.assertEqual(set(view), {
			"name", "file_type", "external_id", "filename", "file_url",
			"is_private", "attached_to_doctype", "attached_to_name",
		})


class TestCallOutputLeg(_TrustedCallerCase):
	def test_get_and_list_agree_on_shape_and_values(self):
		lead = self._lead("+919812390003")
		view = self._hit(
			partner_call.call_create, lead=lead, direction="Inbound",
			from_number="9812390003", to_number="9000000000", external_id="LEG-C1",
		)["data"]

		got = self._hit(partner_call.call_get, name=view["name"])["data"]
		listed = self._hit(partner_call.call_list, lead=lead, limit=10)["data"]["calls"]

		self.assertEqual(len(listed), 1)
		self.assertEqual(got, listed[0], "call_get and call_list disagree on the SAME record")
		self.assertEqual(got, view, "the create response and a fresh read disagree")

	def test_view_shape_is_unchanged(self):
		lead = self._lead("+919812390004")
		view = self._hit(
			partner_call.call_create, lead=lead, direction="Outbound",
			from_number="9000000000", to_number="9812390004",
		)["data"]
		self.assertEqual(set(view), {
			"name", "external_id", "lead", "direction", "from_number", "to_number",
			"status", "duration", "recording_url", "start_time",
		})


class TestNoteOutputLeg(_TrustedCallerCase):
	def test_get_and_list_agree_on_shape_and_values(self):
		lead = self._lead("+919812390005")
		view = self._hit(
			partner_note.note_create, lead=lead, content="<p>leg test</p>", title="Leg Test",
		)["data"]

		got = self._hit(partner_note.note_get, name=view["name"])["data"]
		listed = self._hit(partner_note.note_list, lead=lead, limit=10)["data"]["notes"]

		self.assertEqual(len(listed), 1)
		self.assertEqual(got, listed[0], "note_get and note_list disagree on the SAME record")
		self.assertEqual(got, view, "the create response and a fresh read disagree")

	def test_view_shape_is_unchanged(self):
		lead = self._lead("+919812390006")
		view = self._hit(partner_note.note_create, lead=lead, content="<p>shape</p>")["data"]
		self.assertEqual(set(view), {"name", "external_id", "lead", "title", "content", "created_at"})


# -- GAP 8: create routes through collect(), exactly like update ------------------------------------

class TestNoteCreateRoutesThroughCollect(_TrustedCallerCase):
	def test_create_calls_collect(self):
		"""_create_one used to read data.get(...) raw and never call collect(NOTE_FIELDS, data). Route
		through collect (via _apply_fields, like _update_one does) or this fails."""
		lead = self._lead("+919812390010")
		frappe.form_dict = frappe._dict({"lead": lead, "content": "<p>x</p>"})
		with patch.object(partner_note, "collect", wraps=partner_note.collect) as spy:
			_view, action = partner_note._create_one(frappe.form_dict, self.mp, self.is_sysmgr)
		self.assertEqual(action, ACTION_CREATED)
		self.assertTrue(spy.called, "_create_one never called collect(NOTE_FIELDS, data)")

	def test_create_honors_a_read_only_title_exactly_like_update_does(self):
		"""If `title` were ever marked read_only, `collect` already drops a caller-sent value on
		UPDATE. Create must behave identically — it must not read `title` off raw `data` behind
		collect's back."""
		lead = self._lead("+919812390011")
		locked = tuple(
			s._replace(read_only=True) if s.fieldname == "title" else s
			for s in partner_note.NOTE_FIELDS
		)
		frappe.form_dict = frappe._dict({
			"lead": lead, "title": "Caller Supplied Title", "content": "<p>x</p>",
		})
		with patch.object(partner_note, "NOTE_FIELDS", locked):
			view, _action = partner_note._create_one(frappe.form_dict, self.mp, self.is_sysmgr)
		self.assertNotEqual(
			view["title"], "Caller Supplied Title",
			"create wrote a read_only field straight from data, bypassing collect",
		)
		self.assertEqual(view["title"], "Note")
