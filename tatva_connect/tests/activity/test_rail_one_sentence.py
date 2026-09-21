# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A save is one row with its from -> to, a file is a detail of what it arrived on, and attribution is one read."""

import json
import unittest
from collections import defaultdict
from unittest.mock import patch

import frappe

from tatva_connect.activity import timeline
from tatva_connect.api import activities


def _meta():
	"""The three fields these cases name, in the shape `frappe.get_meta` returns."""
	field = lambda fn, label: type("F", (), {"fieldname": fn, "label": label, "options": None, "fieldtype": "Data"})()  # noqa: E731
	fields = [field("custom_substage", "Sub Stage"), field("status", "Status"), field("custom_stage", "Stage")]
	return type("M", (), {"fields": fields, "get_field": lambda self, fn: next((f for f in fields if f.fieldname == fn), None)})()


def _version(changes, name="VER-1", owner="rep@example.com"):
	# `frappe.get_all` hands `field_changes` a _dict, and it reads `v.data` — the fixture must be the same shape.
	return frappe._dict(name=name, owner=owner, creation="2026-09-09 10:00:00", data=json.dumps({"changed": changes}))


class TestOneSaveIsOneRow(unittest.TestCase):
	"""The rail row a save produces — built once, by the server, for both suppliers."""

	def _row(self, changes):
		with (
			patch("frappe.get_meta", return_value=_meta()),
			patch("tatva_connect.api.activities._full_name", return_value="Asha Rep"),
			patch("tatva_connect.api.activities._stage_label", side_effect=lambda pk: (pk or "").split("::")[-1]),
		):
			return activities._version_row(_version(changes), "CRM Lead")

	def test_a_change_carries_where_it_came_from_and_where_it_went(self):
		row = self._row([["status", "Open", "Contacted"]])

		self.assertEqual(row["changes"], [{"label": "Status", "from": "Open", "to": "Contacted"}])
		self.assertEqual(row["activity_type"], "version")

	def test_one_save_of_many_fields_is_ONE_row(self):
		row = self._row([["status", "Open", "Contacted"], ["custom_substage", "GF::New", "GF::Consulted"]])

		self.assertEqual(len(row["changes"]), 2)
		self.assertEqual(row["name"], "VER-1")

	def test_a_stage_move_reads_as_a_stage_not_a_composite_key(self):
		row = self._row([["custom_substage", "GoodFlip::New", "GoodFlip::Consulted"]])

		self.assertEqual(row["changes"][0]["from"], "New")
		self.assertEqual(row["changes"][0]["to"], "Consulted")

	def test_a_save_of_nothing_readable_is_not_a_row(self):
		self.assertIsNone(self._row([["custom_stage", "GF::A", "GF::B"]]))
		self.assertIsNone(self._row([]))


class TestAFileIsADetail(unittest.TestCase):
	"""A file that arrived on another rail record is that record's detail, never a second row beside it."""

	def test_a_file_on_the_lead_is_an_event(self):
		doc = {"attached_to_doctype": "CRM Lead", "attached_to_name": "LEAD-1"}
		self.assertEqual(timeline._file_lead(doc), ("CRM Lead", "LEAD-1"))

	def test_a_whatsapp_media_file_is_not_a_second_row(self):
		doc = {"attached_to_doctype": "WhatsApp Message", "attached_to_name": "WA-1"}
		self.assertEqual(timeline._file_lead(doc), (None, None))

	def test_an_orphan_file_is_on_no_rail(self):
		self.assertEqual(timeline._file_lead({"attached_to_doctype": "User", "attached_to_name": None}), (None, None))


class TestASaveIsAnEvent(unittest.TestCase):
	def test_version_feeds_the_index(self):
		"""Without this the rail read frappe's ten-version window and could never page past it."""
		self.assertEqual(timeline.SOURCES["Version"], ("version", "docname", "ref_doctype"))

	def test_a_save_that_shows_nothing_is_not_indexed(self):
		"""A pointer must exist exactly when a row renders, or total_count promises rows a page cannot fill."""
		doc = frappe._dict(
			doctype="Version", ref_doctype="CRM Lead", docname="LEAD-1", creation="2026-09-09 10:00:00",
			name="VER-9", data=json.dumps({"changed": [["custom_stage", "GF::A", "GF::B"]]}),
		)
		with (
			patch("frappe.get_meta", return_value=_meta()),
			patch("tatva_connect.activity.timeline._matches", return_value=True),
		):
			self.assertIsNone(timeline.event_row(doc))

	def test_the_writer_and_the_reader_ask_the_same_question(self):
		"""One declaration of what a save shows — not a list here and another one there."""
		from tatva_connect.activity import lead_events

		self.assertIs(activities.rail_changes, lead_events.rail_changes)
		self.assertIs(activities.NOISE_FIELDS, lead_events.NOISE_FIELDS)


class TestASaveIsReadAgainstWhatItWasSavedOn(unittest.TestCase):
	"""A deal's rail carries its LEAD's pointers, and a save can only be read against its own doctype."""

	def test_the_pointer_says_which_record_the_save_belongs_to(self):
		"""Without reference_doctype on the pointer, a lead's save is read as a deal's and finds nothing."""
		import inspect

		src = inspect.getsource(activities._rail_from_index)
		self.assertIn("reference_doctype", src)

	def test_hydrate_reads_each_save_against_its_own_record(self):
		captured = []

		def fake_get_all(source, **kw):
			if source == "Version":
				return [_version([["status", "Open", "Contacted"]], name="VER-1")]
			return []

		with (
			patch("frappe.get_all", side_effect=fake_get_all),
			patch("frappe.get_meta", return_value=_meta()),
			patch("tatva_connect.api.activities._full_name", return_value="Asha Rep"),
			patch("tatva_connect.api.activities._version_row", side_effect=lambda v, dt: captured.append(dt)),
		):
			activities._hydrate([{
				"kind": "version", "source_doctype": "Version", "source_name": "VER-1",
				"event_on": "2026-09-09 10:00:00", "reference_doctype": "CRM Lead",
			}])

		self.assertEqual(captured, ["CRM Lead"])


class TestTheBackfillReadsRowsNotDocuments(unittest.TestCase):
	"""A rebuild over every lead must not load a million documents to read four columns off each."""

	def test_it_asks_for_the_columns_event_row_reads(self):
		for doctype in timeline.SOURCES:
			_kind, link_field, parent_field = timeline.SOURCES[doctype]
			fields = timeline._event_fields(doctype)
			with self.subTest(doctype=doctype):
				self.assertIn(link_field, fields)
				self.assertIn(parent_field, fields)
				self.assertIn("creation", fields)
				for predicate_field in timeline.PREDICATES.get(doctype, {}):
					self.assertIn(predicate_field, fields)

	def test_a_save_is_asked_for_its_payload(self):
		"""`rail_changes` reads the save itself; without these two the rebuild would index nothing."""
		self.assertIn("data", timeline._event_fields("Version"))
		self.assertIn("owner", timeline._event_fields("Version"))

	def test_rows_are_read_in_one_query_per_source(self):
		queries = []

		def fake_get_all(doctype, **kw):
			queries.append(doctype)
			return [{"name": "X1", "creation": "2026-09-09 10:00:00", "reference_docname": "LEAD-1",
					 "reference_doctype": "CRM Lead"}]

		with patch("frappe.get_all", side_effect=fake_get_all):
			rows = timeline._source_rows("FCRM Note", ["LEAD-1"])

		self.assertEqual(queries, ["FCRM Note"])
		self.assertEqual(rows[0].doctype, "FCRM Note")


class TestTheFooterCount(unittest.TestCase):
	"""Most leads hold fewer events than a page, and a short page already knows its own total."""

	def _page(self, pointer_count, page_length):
		pointers = [{"kind": "note", "source_doctype": "FCRM Note", "source_name": f"N{i}",
					 "event_on": "2026-09-09 10:00:00", "reference_doctype": "CRM Lead"}
					for i in range(pointer_count)]
		counted = []
		with (
			patch("frappe.get_all", side_effect=lambda doctype, **kw: pointers if doctype == "CRM Timeline Event" else []),
			patch("tatva_connect.api.activities._hydrate", return_value=[]),
			patch("tatva_connect.api.activities.creation_event", return_value={"creation": "2026-01-01 00:00:00"}),
			patch("tatva_connect.api.activities._scope", return_value=[("CRM Lead", "LEAD-1")]),
		):
			# `frappe.db` is a proxy onto `frappe.local`, so the connection is what a test stands in for.
			had = hasattr(frappe.local, "db")
			previous = frappe.local.db if had else None
			frappe.local.db = frappe._dict(count=lambda *a, **k: counted.append(1) or 99)
			try:
				_rows, total = activities._rail_from_index("LEAD-1", page_length, "creation desc")
			finally:
				if had:
					frappe.local.db = previous
				else:
					del frappe.local.db
		return total, len(counted)

	def test_a_short_page_costs_no_count_query(self):
		total, queries = self._page(pointer_count=3, page_length=20)

		self.assertEqual(queries, 0)
		self.assertEqual(total, 4)

	def test_a_full_page_still_asks_for_the_total(self):
		total, queries = self._page(pointer_count=20, page_length=20)

		self.assertEqual(queries, 1)
		self.assertEqual(total, 100)


class TestWhoMadeTheChange(unittest.TestCase):
	"""A login says which channel made a change, so the rail names the channel rather than a raw user id."""

	def _resolve(self, users, partners=None):
		from tatva_connect.activity import actor

		with (
			patch("tatva_connect.activity.actor.partner_users", return_value=partners or frozenset()),
			patch("frappe.get_all", return_value=[{"name": "asha@x.com", "full_name": "Asha Rep"}]),
		):
			return actor.resolve(users)

	def test_a_guest_is_the_intake_form(self):
		"""Intake is the only path that writes a lead as an anonymous visitor."""
		self.assertEqual(self._resolve(["Guest"])["Guest"], {"label": "Intake form", "kind": "intake"})

	def test_a_partner_key_is_named_by_its_login(self):
		out = self._resolve(["partner-api-niva@x.com"], frozenset({"partner-api-niva@x.com"}))

		self.assertEqual(out["partner-api-niva@x.com"], {"label": "partner-api-niva@x.com · via API", "kind": "api"})

	def test_administrator_is_the_system(self):
		self.assertEqual(self._resolve(["Administrator"])["Administrator"]["kind"], "system")

	def test_a_person_is_their_name(self):
		self.assertEqual(self._resolve(["asha@x.com"])["asha@x.com"], {"label": "Asha Rep", "kind": "person"})

	def test_the_contract_list_is_read_once_per_request(self):
		"""Fifteen rows, and `_stage_moved` names an actor per row — so it is read once, not per call."""
		from tatva_connect.activity import actor

		reads = []
		# The shape frappe builds at request start; outside a request the decorator simply calls through.
		frappe.local.request_cache = defaultdict(dict)
		with patch("frappe.get_all", side_effect=lambda *a, **k: reads.append(1) or []):
			actor.partner_users()
			actor.partner_users()
			actor.partner_users()
		frappe.local.request_cache = defaultdict(dict)

		self.assertEqual(len(reads), 1)

	def test_no_user_read_when_no_login_is_a_person(self):
		from tatva_connect.activity import actor

		with (
			patch("tatva_connect.activity.actor.partner_users", return_value=frozenset()),
			patch("frappe.get_all", side_effect=AssertionError("must not read User")),
		):
			out = actor.resolve(["Guest", "Administrator", None, ""])

		self.assertEqual(set(out), {"Guest", "Administrator"})

	def test_the_page_is_named_once_not_per_row(self):
		calls = []

		def fake_resolve(users):
			calls.append(sorted({u for u in users if u}))
			return {"Guest": {"label": "Intake form", "kind": "intake"}}

		rows = [{"owner": "Guest"}, {"owner": "Guest"}, {"owner": None}]
		with patch("tatva_connect.activity.actor.resolve", side_effect=fake_resolve):
			out = activities._name_actors(rows)

		self.assertEqual(calls, [["Guest"]])
		self.assertEqual(out[0]["owner_name"], "Intake form")
		self.assertEqual(out[0]["owner_kind"], "intake")
		self.assertNotIn("owner_name", out[2])


class TestAttributionIsOneRead(unittest.TestCase):
	"""A page of twenty tasks must not cost forty round trips to answer 'was this automation'."""

	def test_many_rows_resolve_in_two_queries(self):
		from tatva_connect.automation import origin

		calls = []

		def fake_get_all(doctype, **_kw):
			calls.append(doctype)
			if doctype == "CRM Task":
				return [{"name": "T1", "custom_workflow_token": "J1::demo_task"}, {"name": "T2", "custom_workflow_token": None}]
			return [{"name": "J1", "workflow": "Welcome Call"}]

		with patch("frappe.get_all", side_effect=fake_get_all):
			out = origin.automation_origins("CRM Task", ["T1", "T2"])

		self.assertEqual(len(calls), 2)
		self.assertEqual(out, {"T1": {"label": "Welcome Call", "journey": "J1"}})

	def test_nothing_to_resolve_costs_nothing(self):
		from tatva_connect.automation import origin

		with patch("frappe.get_all", side_effect=AssertionError("must not query")):
			self.assertEqual(origin.automation_origins("CRM Task", []), {})
			self.assertEqual(origin.automation_origins("FCRM Note", ["N1"]), {})
