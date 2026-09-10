# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every partner entity, both lanes, driven as a real partner key.

THE MATRIX is (family x lane x verb). Families are the five a partner addresses; lanes are single and
bulk; verbs are the lifecycle each family declares. The verb set is DISCOVERED per family — `file` has no
update, and nothing here says so — so a family that gains or loses an endpoint changes what runs without
this file being edited.

The caller is the GoodFlip key, not Administrator: the mapping forces the grain and the source, the
contract decides the field list, and the limiter meters the call. A test that drives this as a System
Manager proves the endpoint parses its own arguments and nothing else.

Every write rolls back.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.api.test_partner_matrix
"""
import time
import unittest
from collections import namedtuple
from contextlib import contextmanager

import frappe

from tatva_connect.activity import api as activity_brain
from tatva_connect.api import partner, partner_activity, partner_call, partner_file, partner_note
from tatva_connect.api._base import ACTION_CREATED, ACTION_DELETED, ACTION_FETCHED, ACTION_UPDATED
from tatva_connect.tests.api.partner_fixture import minimal_answers

PARTNER = "partner-api-gfis@tatvacare.in"

Family = namedtuple("Family", "name module collection create needs_lead")

FAMILIES = (
	Family("lead", partner, "leads", "lead_create", False),
	Family("activity", partner_activity, "activities", "activity_create", True),
	Family("call", partner_call, "calls", "call_create", True),
	Family("note", partner_note, "notes", "note_create", True),
	Family("file", partner_file, "files", "file_attach", True),
)

SINGLE = ("schema", "create", "get", "update", "list", "delete")
BULK = ("create", "get", "update", "delete")


def endpoint(family, verb, lane):
	"""The function a (family, verb, lane) names, or None where the family declares no such endpoint.

	`create` is the family's own word for it — a file is attached, not created — and every other verb is
	`<family>_<verb>`. Discovered, so `file`'s missing update is absent from the matrix rather than
	excluded by a rule someone has to remember."""
	stem = family.create if verb == "create" else f"{family.name}_{verb}"
	name = f"{stem}_bulk" if lane == "bulk" else stem
	return getattr(family.module, name, None)


class PartnerMatrixCase(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not frappe.db.exists("CRM Lead API Mapping", {"partner_user": PARTNER, "enabled": 1}):
			raise unittest.SkipTest(f"no enabled mapping for {PARTNER} on this bench")
		frappe.set_user(PARTNER)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")

	def setUp(self):
		self._form = frappe.form_dict
		self.minted = 0

	def tearDown(self):
		frappe.form_dict = self._form
		frappe.db.rollback()

	def new_phone(self):
		"""A number no lead holds. Minted per lead, never per test: a lead is deduped on phone + grain, so
		reusing one turns the next create into an update and the matrix stops testing create at all."""
		self.minted += 1
		return f"+9198{int(time.time() * 100) % 1000000:06d}{self.minted:02d}"

	def hit(self, fn, **args):
		"""Drive one endpoint the way the HTTP layer does, and hand back the envelope it wrote.

		A `rate_limited` refusal is honoured once rather than failed: the bulk bucket holds ONE call and
		is pinned there so two concurrent bulk writes cannot deadlock on the lead dedup index, so a matrix
		walking every family back to back meets it by design. Retrying on the advertised `retry_after` is
		what a client does, and it puts that header under test instead of working around it."""
		for attempt in (1, 2):
			frappe.local.response = frappe._dict()
			frappe.form_dict = frappe._dict(args)
			fn()
			answer = dict(frappe.local.response)
			error = answer.get("error") or {}
			if attempt == 2 or error.get("code") != "rate_limited":
				return answer
			time.sleep((error.get("retry_after") or 1) + 0.5)

	def ok(self, answer, where, action=None):
		self.assertEqual(answer.get("status"), "success",
		                 f"{where}: {(answer.get('error') or {}).get('message', answer)}")
		if action:
			self.assertEqual(answer.get("action"), action, f"{where}: wrong action")
		return answer

	def bulk_ok(self, answer, where, count=1):
		"""A bulk call is a 200 whatever its records did, so the SUMMARY is the verdict, not the status."""
		self.ok(answer, where)
		summary = answer.get("summary") or {}
		self.assertEqual(summary.get("total"), count, f"{where}: summary counts the wrong number")
		self.assertEqual(summary.get("failed"), 0,
		                 f"{where}: {summary.get('failed')} of {count} failed -> {answer.get('results')}")
		return answer["results"]

	# -- the fixtures each family needs -------------------------------------------------------------

	# An activity type is scoped to a program, which a partner-created lead has not got.
	def a_lead(self):
		return self.ok(self.hit(partner.lead_create, mobile_no=self.new_phone(), first_name="Matrix"),
		               "lead_create", ACTION_CREATED)["data"]["name"]

	@contextmanager
	def as_operator(self):
		"""Choosing a fixture is not the thing under test. The brain answers `list_types_for_lead` through
		the permission layer, which refuses a partner key reading CRM Lead directly — correctly, since a
		partner reaches a lead only through an endpoint. So the SETUP asks as an operator; every call the
		matrix actually asserts on is made as the partner."""
		frappe.set_user("Administrator")
		try:
			yield
		finally:
			frappe.set_user(PARTNER)

	def a_programmed_lead(self):
		with self.as_operator():
			for name in frappe.get_all(
				"CRM Lead", filters={"custom_vertical": "Goodflip", "custom_group": "India"},
				pluck="name", limit=200,
			):
				if activity_brain.list_types_for_lead(name):
					return name
		return None

	def a_task_type(self, lead):
		with self.as_operator():
			types = activity_brain.list_types_for_lead(lead)
		return types[0]["name"] if types else None

	def record(self, family, lead):
		"""One record of `family`, as the caller would send it."""
		if family.name == "lead":
			return {"mobile_no": self.new_phone(), "first_name": "Matrix"}
		if family.name == "activity":
			# An activity is a form: its type declares what is required, so a create sending none is refused.
			task_type = self.a_task_type(lead)
			return {"lead": lead, "task_type": task_type, "values": minimal_answers(task_type)}
		if family.name == "call":
			return {"lead": lead, "direction": "Inbound",
			        "from_number": "9812300000", "to_number": "9000000000"}
		if family.name == "note":
			return {"lead": lead, "content": "matrix probe"}
		return {"lead": lead, "filename": "matrix.txt", "content_base64": "cHJvYmU="}

	def edit(self, family, name, lead):
		"""The same record, changed — what an update sends."""
		if family.name == "lead":
			return {"name": name, "last_name": "Edited"}
		if family.name == "activity":
			task_type = self.a_task_type(lead)
			return {"name": name, "task_type": task_type, "values": minimal_answers(task_type)}
		if family.name == "call":
			return {"name": name, "status": "Completed", "duration": 42}
		return {"name": name, "content": "matrix probe, edited"}

	def lead_for(self, family):
		if not family.needs_lead:
			return None
		lead = self.a_programmed_lead() if family.name == "activity" else self.a_lead()
		if lead is None:
			self.skipTest("no Goodflip lead on this bench runs an activity type")
		return lead


class TestSingleLane(PartnerMatrixCase):
	"""schema -> create -> get -> update -> list -> delete, per family."""

	def test_the_single_lane_lifecycle(self):
		for family in FAMILIES:
			with self.subTest(family=family.name):
				lead = self.lead_for(family)
				verbs = {v: endpoint(family, v, "single") for v in SINGLE}

				schema_args = {"lead": lead} if family.needs_lead else {}
				self.ok(self.hit(verbs["schema"], **schema_args), f"{family.name}_schema", ACTION_FETCHED)

				created = self.ok(self.hit(verbs["create"], **self.record(family, lead)),
				                  f"{family.name} create")
				name = created["data"]["name"]

				self.ok(self.hit(verbs["get"], name=name), f"{family.name} get", ACTION_FETCHED)

				if verbs["update"]:
					self.ok(self.hit(verbs["update"], **self.edit(family, name, lead)),
					        f"{family.name} update", ACTION_UPDATED)

				list_args = {"lead": lead} if family.needs_lead else {}
				listed = self.ok(self.hit(verbs["list"], limit=5, **list_args),
				                 f"{family.name} list", ACTION_FETCHED)["data"]
				for key in ("total", "count", "offset", "limit", "has_more", family.collection):
					self.assertIn(key, listed, f"{family.name} list is missing `{key}`")

				self.ok(self.hit(verbs["delete"], name=name), f"{family.name} delete", ACTION_DELETED)


class TestBulkLane(PartnerMatrixCase):
	"""create_bulk -> get_bulk -> update_bulk -> delete_bulk, per family."""

	def test_the_bulk_lane_lifecycle(self):
		for family in FAMILIES:
			with self.subTest(family=family.name):
				lead = self.lead_for(family)
				verbs = {v: endpoint(family, v, "bulk") for v in BULK}
				keys = self.ok(self.hit(endpoint(family, "schema", "single"),
				                        **({"lead": lead} if family.needs_lead else {})),
				               f"{family.name}_schema")["data"]["bulk"]["payload_key"]

				results = self.bulk_ok(
					self.hit(verbs["create"], **{keys["create"]: [self.record(family, lead)]}),
					f"{family.name} create_bulk")
				name = results[0]["data"]["name"]

				self.bulk_ok(self.hit(verbs["get"], **{keys["get"]: [name]}), f"{family.name} get_bulk")

				if verbs["update"]:
					self.bulk_ok(self.hit(verbs["update"],
					                      **{keys["update"]: [self.edit(family, name, lead)]}),
					             f"{family.name} update_bulk")

				self.bulk_ok(self.hit(verbs["delete"], **{keys["delete"]: [name]}),
				             f"{family.name} delete_bulk")


class TestTheMatrixIsComplete(unittest.TestCase):
	"""The matrix must cover what the code exposes, so a new endpoint is not silently untested."""

	def test_every_declared_endpoint_is_reachable_from_the_matrix(self):
		driven = (endpoint(f, v, lane)
		          for f in FAMILIES
		          for lane, verbs in (("single", SINGLE), ("bulk", BULK))
		          for v in verbs)
		covered = {fn.__name__ for fn in driven if fn}
		declared = {
			name
			for f in FAMILIES
			for name in dir(f.module)
			if getattr(getattr(f.module, name), "_partner_lane", None) is not None
		}
		self.assertEqual(declared - covered, set(),
		                 "these partner endpoints are declared but the matrix never drives them")
