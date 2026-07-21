# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The partner API's identity contract — the four axioms, pinned.

These are the rules the whole surface is derived from. If one of these tests goes red, the API is
speaking a second language again:

  1. The primary key of the underlying table is the anchor. A create returns `name`; every
     subsequent read, update and delete is addressed by it. It is the ONLY address.
  2. Deduplication is OURS, never the caller's. A lead is one per (mobile_no, vertical, group). An
     activity, file or call is CREATED — a re-sent create produces a second record.
  3. `external_id` is a cosmetic label. It is stored and echoed back, never interpreted, and never
     resolves a record. A colliding label is inert.
  4. One envelope. Every write returns the full record; every bulk (read AND write) returns the same
     {total, succeeded, failed} summary; every list returns the same page shape.

The caller here is a TRUSTED System Manager (no mapping) — the grain-scoped partner path has its own
adversarial coverage under tatva_connect/tests/authz (A13), which is the differential/metamorphic
framework and stays the one place scoping is proven. This module pins the CONTRACT, not the gate.
"""
import unittest
from unittest.mock import patch

import frappe

from tatva_connect.activity import api as activity_brain
from tatva_connect.api import _base, partner, partner_activity, partner_call, partner_file, partner_note
from tatva_connect.api._base import (
	ACTION_CREATED,
	ACTION_DELETED,
	ACTION_FETCHED,
	ACTION_UPDATED,
	EXTERNAL_ID_FIELD,
	_api,
	_bulk_read,
	_list_ok,
	_run_bulk,
)

VERTICAL, GROUP = "GoodFlip Care", "Anaya"
BULK_SUMMARY_KEYS = ["failed", "succeeded", "total"]


class TestPartnerContract(unittest.TestCase):
	"""Trusted caller (System Manager, no mapping) — unscoped, so the contract is isolated from the gate."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.mp, cls.is_sysmgr = None, True

	def setUp(self):
		self.sp = f"partner_contract_{frappe.generate_hash(length=6)}"
		frappe.db.savepoint(self.sp)
		self._form = frappe.form_dict
		frappe.local.response = frappe._dict()

	def tearDown(self):
		frappe.form_dict = self._form
		try:
			frappe.db.rollback(save_point=self.sp)
		except Exception:
			frappe.db.rollback()  # a test that asserts the FULL rollback discarded the savepoint; nothing to preserve

	# -- helpers -------------------------------------------------------------

	def _lead(self, phone, external_id=None):
		"""Upsert a lead through the real core and return (doc, action)."""
		frappe.form_dict = frappe._dict({
			"mobile_no": phone, "first_name": "Contract Test",
			"custom_vertical": VERTICAL, "custom_group": GROUP,
		})
		if external_id is not None:
			frappe.form_dict["external_id"] = external_id
		_u, mp, s, pf, ca = partner._caller_fields()
		return partner._upsert_one(frappe.form_dict, mp, s, pf, ca, [])

	def _call(self, lead, external_id=None, **extra):
		data = {"lead": lead, "direction": "Inbound",
		        "from_number": "9812300077", "to_number": "9999999999", **extra}
		if external_id is not None:
			data["external_id"] = external_id
		frappe.form_dict = frappe._dict(data)
		return partner_call._create_one(frappe.form_dict, self.mp, self.is_sysmgr)

	def _partner_note(self, lead, external_id=None, **extra):
		# NOT `_note` — that name is taken by the raw-doc helper the file-homing test uses.
		data = {"lead": lead, "content": "<p>contract test note</p>", **extra}
		if external_id is not None:
			data["external_id"] = external_id
		frappe.form_dict = frappe._dict(data)
		return partner_note._create_one(frappe.form_dict, self.mp, self.is_sysmgr)

	# -- axiom 2: dedup is OURS ---------------------------------------------

	def test_lead_dedups_on_phone_and_grain_not_on_external_id(self):
		"""The SAME phone with a DIFFERENT external_id is the SAME lead. The label never splits a lead."""
		phone = "+919812300081"
		doc1, action1 = self._lead(phone, external_id="PARTNER-A")
		doc2, action2 = self._lead(phone, external_id="PARTNER-B-DIFFERENT")

		self.assertEqual(action1, ACTION_CREATED)
		self.assertEqual(action2, ACTION_UPDATED, "a re-sent phone must UPDATE, never create a second lead")
		self.assertEqual(doc1.name, doc2.name, "dedup must be (mobile_no, vertical, group) — NOT external_id")
		self.assertEqual(
			doc2.get(EXTERNAL_ID_FIELD), "PARTNER-B-DIFFERENT",
			"the label is overwritten by the latest write; it is cosmetic and carries no identity",
		)

	def test_a_differing_external_id_never_splits_a_lead(self):
		"""Two DIFFERENT labels on one phone still resolve to ONE lead — the label is not a key."""
		phone = "+919812300082"
		first, _ = self._lead(phone, external_id="X-1")
		second, _ = self._lead(phone, external_id="X-2")
		self.assertEqual(first.name, second.name)
		self.assertEqual(
			frappe.db.count("CRM Lead", {"mobile_no": phone, "custom_vertical": VERTICAL,
			                             "custom_group": GROUP}),
			1, "exactly ONE lead may exist per (phone, vertical, group)",
		)

	def test_call_create_creates_it_does_not_upsert(self):
		"""A re-sent create with the SAME external_id produces a SECOND call. There is no upsert."""
		lead, _ = self._lead("+919812300083")
		one, a1 = self._call(lead.name, external_id="SAME-LABEL")
		two, a2 = self._call(lead.name, external_id="SAME-LABEL")

		self.assertEqual(a1, ACTION_CREATED)
		self.assertEqual(a2, ACTION_CREATED, "a create CREATES — it never silently updates on a label")
		self.assertNotEqual(
			one["name"], two["name"],
			"a re-sent create must mint a SECOND record; external_id must not deduplicate",
		)
		self.assertEqual(one["external_id"], two["external_id"], "both carry the label they were sent")

	# -- axiom 1 + 3: name is the anchor, external_id is inert ---------------

	def test_a_record_is_addressed_by_name(self):
		"""Create -> name; read, update and delete all address by that name."""
		lead, _ = self._lead("+919812300084")
		created, _ = self._call(lead.name, external_id="ANCHOR")
		name = created["name"]

		fetched = partner_call._read_one(name, self.mp, self.is_sysmgr)
		self.assertEqual(fetched["name"], name)

		frappe.form_dict = frappe._dict({"name": name, "status": "Completed", "duration": 187})
		updated, action = partner_call._update_one(name, frappe.form_dict, self.mp, self.is_sysmgr)
		self.assertEqual(action, ACTION_UPDATED)
		self.assertEqual(updated["name"], name, "an update never re-mints the anchor")
		self.assertEqual(int(updated["duration"]), 187)

		partner_call._delete_one(name, self.mp, self.is_sysmgr)
		self.assertFalse(frappe.db.exists("CRM Call Log", name))

	def test_every_entity_returns_a_STRING_address(self):
		"""One address means one TYPE. CRM Task is autoincrement-named, so its PK is an int, and the
		activity payload handed back a number while lead/call/file handed back strings -- against an
		OpenAPI that declares `name` a string. A generated typed client breaks on the wire."""
		lead, _ = self._lead("+919812300104")
		call, _ = self._call(lead.name)
		file_view, _ = self._attach(lead.name)
		note, _ = self._partner_note(lead.name)

		addresses = {"lead": lead.name, "call": call["name"], "file": file_view["name"],
		             "note": note["name"]}

		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict({"lead": lead.name})
		partner_activity.activity_schema()
		types = frappe.local.response["data"]["task_types"]
		if types:
			activity = partner_activity._create_one(
				frappe._dict({"lead": lead.name, "task_type": types[0]["name"], "values": {}}),
				self.mp, self.is_sysmgr)
			addresses["activity"] = activity["name"]

		for entity, name in addresses.items():
			with self.subTest(entity=entity):
				self.assertIsInstance(
					name, str,
					f"{entity} returned a {type(name).__name__} address; every entity must return a "
					f"string, and the OpenAPI declares `name` a string",
				)

	def test_note_create_creates_it_does_not_upsert(self):
		"""A note follows the SAME rule as every other sub-entity: a POST creates. The label never
		deduplicates, so a re-sent create is a SECOND note, and a retry is made safe with Idempotency-Key."""
		lead, _ = self._lead("+919812300110")
		first, a1 = self._partner_note(lead.name, external_id="SAME-LABEL")
		second, a2 = self._partner_note(lead.name, external_id="SAME-LABEL")
		self.assertEqual((a1, a2), (ACTION_CREATED, ACTION_CREATED))
		self.assertNotEqual(first["name"], second["name"],
		                    "external_id must never deduplicate a note")

	def test_a_note_is_never_orphaned_or_guessed_onto_a_lead(self):
		"""A call that cannot be attributed is kept unlinked — a note is REFUSED. A clinical note on the
		wrong patient, or on none, is worse than a refused write, so notes have no phone-guess fallback."""
		frappe.form_dict = frappe._dict({"content": "<p>no lead named</p>"})
		with self.assertRaises(
			Exception,
			msg="a note that names no reachable lead must be refused, never left unattached",
		):
			partner_note._create_one(frappe.form_dict, self.mp, self.is_sysmgr)

	def test_a_note_title_is_built_never_derived_from_the_body(self):
		"""FCRM Note.title is mandatory but OPTIONAL in the contract: a caller with no title concept gets
		a metadata header. It must never be the body echoed back — that would just print the note twice."""
		lead, _ = self._lead("+919812300111")
		view, _ = self._partner_note(lead.name, content="<p>Patient reports fatigue.</p>",
		                     created_at="2026-05-30 10:15:00")
		self.assertTrue(view["title"], "a note always carries a title")
		self.assertNotIn("<p>", view["title"], "the title must never be derived from the content")
		self.assertNotIn("fatigue", view["title"].lower())

	def test_external_id_is_stored_echoed_and_never_resolves(self):
		"""The label round-trips on every read, and is NOT an address: no endpoint takes it."""
		lead, _ = self._lead("+919812300085")
		created, _ = self._call(lead.name, external_id="ECHO-ME")
		self.assertEqual(created["external_id"], "ECHO-ME")
		self.assertEqual(
			partner_call._read_one(created["name"], self.mp, self.is_sysmgr)["external_id"], "ECHO-ME")

		# The label is NOT an address. _read_one takes a `name`; handed the label, it must not resolve.
		with self.assertRaises(
			frappe.DoesNotExistError,
			msg="external_id must never resolve a record — `name` is the only address",
		):
			partner_call._read_one("ECHO-ME", self.mp, self.is_sysmgr)

	def test_external_id_may_be_omitted_entirely(self):
		"""The label is OPTIONAL on every entity. A create without one succeeds and echoes null."""
		lead, _ = self._lead("+919812300086")          # no external_id
		created, action = self._call(lead.name)        # no external_id
		self.assertEqual(action, ACTION_CREATED)
		self.assertIsNone(created["external_id"])
		self.assertIsNone(lead.get(EXTERNAL_ID_FIELD))

	# -- axiom 4: one envelope ----------------------------------------------

	def test_every_bulk_read_and_write_shares_one_summary_shape(self):
		"""Reads and writes emit the SAME {total, succeeded, failed}. No endpoint invents its own."""
		lead, _ = self._lead("+919812300087")
		one, _ = self._call(lead.name)
		two, _ = self._call(lead.name)

		# READ lane — two real names plus one that does not exist.
		frappe.local.response = frappe._dict()
		_bulk_read([one["name"], two["name"], "DOES-NOT-EXIST"],
		           lambda n: partner_call._read_one(n, self.mp, self.is_sysmgr))
		read = frappe.local.response
		self.assertEqual(sorted(read["summary"]), BULK_SUMMARY_KEYS)
		self.assertEqual(dict(read["summary"]), {"total": 3, "succeeded": 2, "failed": 1})
		self.assertEqual([r["index"] for r in read["results"]], [0, 1, 2], "results are input-ordered")
		self.assertEqual(read["results"][0]["action"], ACTION_FETCHED)
		self.assertEqual(read["results"][2]["error"]["code"], "not_found")
		self.assertNotIn("data", read["results"][2], "a failed row carries an error, never data")

		# WRITE lane — the SAME summary keys.
		frappe.local.response = frappe._dict()

		def delete(i, name):
			partner_call._delete_one(name, self.mp, self.is_sysmgr)
			return {"index": i, "status": "success", "action": ACTION_DELETED, "data": {"name": name}}

		_run_bulk([one["name"], "DOES-NOT-EXIST"], delete)
		write = frappe.local.response
		self.assertEqual(sorted(write["summary"]), BULK_SUMMARY_KEYS,
		                 "the write lane must emit the SAME three keys as the read lane")
		self.assertEqual(dict(write["summary"]), {"total": 2, "succeeded": 1, "failed": 1})

	def test_a_failing_bulk_row_does_not_roll_back_the_others(self):
		"""Partial success: one bad row reports its error, the rest still commit."""
		lead, _ = self._lead("+919812300088")
		good, _ = self._call(lead.name)
		frappe.local.response = frappe._dict()

		def delete(i, name):
			partner_call._delete_one(name, self.mp, self.is_sysmgr)
			return {"index": i, "status": "success", "action": ACTION_DELETED, "data": {"name": name}}

		_run_bulk(["DOES-NOT-EXIST", good["name"]], delete)
		self.assertEqual(dict(frappe.local.response["summary"]), {"total": 2, "succeeded": 1, "failed": 1})
		self.assertFalse(frappe.db.exists("CRM Call Log", good["name"]),
		                 "the good row must still have committed despite the bad one")

	def test_every_list_emits_one_page_shape(self):
		"""total / count / offset / limit / has_more, on every entity, with no exceptions."""
		frappe.local.response = frappe._dict()
		_list_ok("calls", [{"name": "a"}, {"name": "b"}], total=5, offset=0, limit=2)
		data = frappe.local.response["data"]
		self.assertEqual(frappe.local.response["action"], ACTION_FETCHED)
		self.assertEqual(
			sorted(data), ["calls", "count", "has_more", "limit", "offset", "total"],
			"every list emits the same five page keys plus its own collection",
		)
		self.assertTrue(data["has_more"], "offset 0 + 2 of 5 rows means more remain")

		frappe.local.response = frappe._dict()
		_list_ok("calls", [{"name": "e"}], total=5, offset=4, limit=2)
		self.assertFalse(frappe.local.response["data"]["has_more"], "the last page reports has_more false")

	def test_a_write_returns_the_full_record(self):
		"""A create, an update and a get hand back the SAME shape — they can never diverge."""
		lead, _ = self._lead("+919812300089", external_id="SHAPE")
		created, _ = self._call(lead.name, external_id="SHAPE")
		fetched = partner_call._read_one(created["name"], self.mp, self.is_sysmgr)
		self.assertEqual(
			sorted(created), sorted(fetched),
			"the create response and the get response must be the same shape",
		)

	# -- P4: one transaction boundary ---------------------------------------

	def test_a_rejected_write_leaves_nothing_behind(self):
		"""A throw AFTER a row has landed must roll the row back. @_api swallows the exception, so the
		request ends normally and Frappe would otherwise commit the failed write."""
		lead, _ = self._lead("+919812300103")
		landed = {}

		@_api
		def probe(**_kwargs):
			# A REAL create, through the real core -- so a row genuinely exists when the throw fires.
			view, _action = partner_call._create_one(
				frappe._dict({"lead": lead.name, "direction": "Inbound",
				              "from_number": "9812300077", "to_number": "9999999999"}),
				self.mp, self.is_sysmgr)
			landed["name"] = view["name"]
			self.assertTrue(frappe.db.exists("CRM Call Log", view["name"]), "the row must exist here")
			frappe.throw("rejected after the row landed")

		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict()
		probe()

		self.assertEqual(frappe.local.response["status"], "error", "the caller must be told it failed")
		self.assertIn("name", landed, "the probe must have actually written a row")
		self.assertFalse(
			frappe.db.exists("CRM Call Log", landed["name"]),
			"a rejected write must leave NOTHING behind -- the caller was told 'nothing was created'",
		)

	# -- P6: one meter -------------------------------------------------------

	def test_every_list_charges_its_true_row_count(self):
		"""Every list meters the rows it actually returned, in the read direction. The charge lives in
		_list_ok, so a list endpoint cannot be written that forgets it."""
		lead, _ = self._lead("+919812300091")
		self._call(lead.name)
		self._call(lead.name)

		lists = (
			(partner_call.call_list, {"lead": lead.name}, 2),
			(partner_file.file_list, {"lead": lead.name}, 0),
			(partner_activity.activity_list, {"lead": lead.name}, 0),
		)
		for endpoint, args, expected_rows in lists:
			with self.subTest(endpoint=endpoint.__name__):
				charged = []
				with patch.object(_base, "_meter_volume",
				                  side_effect=lambda rows, direction: charged.append((rows, direction))):
					frappe.local.response = frappe._dict()
					frappe.form_dict = frappe._dict(args)
					endpoint()
				self.assertEqual(
					charged, [(expected_rows, "read")],
					f"{endpoint.__name__} must charge its TRUE row count once, in the read direction",
				)

	def test_a_list_charges_rows_returned_not_the_page_size_requested(self):
		"""limit=200 over a lead holding 2 calls costs 2 rows, not 200."""
		lead, _ = self._lead("+919812300092")
		self._call(lead.name)
		self._call(lead.name)

		charged = []
		with patch.object(_base, "_meter_volume",
		                  side_effect=lambda rows, direction: charged.append((rows, direction))):
			frappe.local.response = frappe._dict()
			frappe.form_dict = frappe._dict({"lead": lead.name, "limit": 200})
			partner_call.call_list()

		self.assertEqual(charged, [(2, "read")], "an over-wide limit must not be billed as rows read")

	# -- P10: the lane is declared, not sniffed ------------------------------

	def test_a_bulk_read_never_claims_an_idempotency_key(self):
		"""The four *_get_bulk are READS exposed over POST. Sniffing the verb would drop them into the
		idempotency lane, so an SDK that stamps a key on every POST would replay a stale snapshot."""
		lead, _ = self._lead("+919812300093")
		one, _ = self._call(lead.name)

		for endpoint, args, should_claim in (
			(partner_call.call_get_bulk, {"names": [one["name"]]}, False),
			(partner_call.call_create, {"lead": lead.name, "direction": "Inbound"}, True),
		):
			with self.subTest(endpoint=endpoint.__name__):
				claimed = []
				with patch.object(_base, "_idem_key", return_value="SDK-STAMPS-EVERY-POST"), \
				     patch.object(_base, "_idempotency_begin",
				                  side_effect=lambda u, k, f: claimed.append(f) or ("run", None)):
					frappe.local.response = frappe._dict()
					frappe.form_dict = frappe._dict(args)
					endpoint()
				self.assertEqual(
					bool(claimed), should_claim,
					f"{endpoint.__name__}: a read must NOT claim an idempotency key; a write MUST",
				)

	def test_the_declared_lane_matches_the_http_verb(self):
		"""DRIFT LOCK. Every endpoint declares its lane on @_api; the declaration must agree with the
		verb it is whitelisted under. A GET that forgets read=True would charge WRITE volume and claim
		idempotency keys; a POST that wrongly claims read=True would skip the write meter. The four
		*_get_bulk are the only reads exposed over POST and are named here on purpose -- a fifth one
		cannot be added silently."""
		reads_over_post = {"lead_get_bulk", "activity_get_bulk", "file_get_bulk", "call_get_bulk"}
		modules = (partner, partner_activity, partner_file, partner_call)
		checked = 0

		for module in modules:
			for name in dir(module):
				fn = getattr(module, name)
				lane = getattr(fn, "_partner_lane", None)
				if lane is None:
					continue
				verbs = frappe.allowed_http_methods_for_whitelisted_func.get(fn)
				self.assertTrue(verbs, f"{name} carries @_api but is not whitelisted")
				expected_read = verbs == ["GET"] or name in reads_over_post
				self.assertEqual(
					lane["read"], expected_read,
					f"{name} is whitelisted {verbs} but declares read={lane['read']}. "
					f"A read must declare read=True; a write must not.",
				)
				checked += 1

		self.assertEqual(checked, 38, "every partner endpoint must carry a declared lane")

	# -- P1: one address. Every home the write accepts, the read must honour -

	def _note(self, lead):
		return frappe.get_doc({
			"doctype": "FCRM Note", "title": "contract-test",
			"reference_doctype": "CRM Lead", "reference_docname": lead,
		}).insert(ignore_permissions=True)

	def _task(self, lead):
		return frappe.get_doc({
			"doctype": "CRM Task", "title": "contract-test", "status": "Backlog",
			"reference_doctype": "CRM Lead", "reference_docname": lead,
		}).insert(ignore_permissions=True)

	def _attach(self, lead, **extra):
		frappe.form_dict = frappe._dict({
			"lead": lead, "filename": f"probe-{frappe.generate_hash(length=6)}.txt",
			"content_base64": "cHJvYmU=", **extra,
		})
		return partner_file._create_one(frappe.form_dict, self.mp, self.is_sysmgr)

	def test_every_home_the_attach_accepts_is_readable_listable_and_deletable(self):
		"""THE ADDRESS AXIOM. _resolve_target knew three homes, _file_lead knew two and file_list knew
		one, so a file attached to a note came back with a `name` that every read, list and delete then
		404'd. The API must never mint an address it refuses to honour."""
		lead, _ = self._lead("+919812300094")
		task = self._task(lead.name)
		note = self._note(lead.name)

		homes = {
			"lead": self._attach(lead.name)[0],
			"activity": self._attach(lead.name, activity=task.name)[0],
			"note": self._attach(lead.name, note=note.name)[0],
		}

		for home, view in homes.items():
			with self.subTest(home=home):
				name = view["name"]
				# readable by the address the create handed back
				self.assertEqual(
					partner_file._read_one(name, self.mp, self.is_sysmgr)["name"], name,
					f"a file homed on a {home} is unreadable by the name its own create returned",
				)

		# every one of them is enumerable -- file_list is the ONLY enumeration surface the API has
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict({"lead": lead.name, "limit": 50})
		partner_file.file_list()
		listed = {f["name"] for f in frappe.local.response["data"]["files"]}
		for home, view in homes.items():
			with self.subTest(home=home):
				self.assertIn(
					view["name"], listed,
					f"a file homed on a {home} is invisible to file_list -- a partner reconciling a "
					f"crashed batch can never rediscover it and re-uploads duplicates instead",
				)
		self.assertEqual(frappe.local.response["data"]["total"], 3, "total must count every home")

		# and deletable
		for home, view in homes.items():
			with self.subTest(home=home):
				partner_file._delete_one(view["name"], self.mp, self.is_sysmgr)
				self.assertFalse(frappe.db.exists("File", view["name"]))

	def test_the_target_table_is_the_one_source_for_write_read_and_enumeration(self):
		"""DRIFT LOCK. A home added to the write side without the read side would mint unaddressable
		records again. All three sides walk _TARGETS."""
		self.assertEqual(
			partner_file._TARGET_DOCTYPES,
			tuple(dt for _k, dt in partner_file._TARGETS),
			"the read side must be derived from the write side, never re-listed",
		)
		# every target must carry reference_doctype/reference_docname, which is what makes a file
		# homed on it resolvable back to its lead.
		for _key, doctype in partner_file._TARGETS:
			with self.subTest(doctype=doctype):
				meta = frappe.get_meta(doctype)
				self.assertTrue(meta.get_field("reference_doctype"))
				self.assertTrue(meta.get_field("reference_docname"))

	# -- P8/P9: create and update resolve the program through ONE brain -------

	def test_an_update_transitions_the_program_through_the_same_brain_as_a_create(self):
		"""lead_create resolved custom_current_program through _resolve_program; lead_update did not
		read it at all, so a list-mode key sent a transition, got 200 updated, and the program never
		changed -- while lead_schema advertised the field as writable."""
		lead, _ = self._lead("+919812300105")
		programs = frappe.get_all("CRM Program", pluck="name", order_by="name", limit=2)
		if len(programs) < 2:
			self.skipTest("need two CRM Programs to test a transition")
		first, second = programs

		# a LIST-mode key: line + group fixed, program chosen per lead from allowed_programs
		mp = frappe._dict({"source": None, "vertical": VERTICAL, "crm_group": GROUP, "program": None})
		_u, _m, _s, pf, ca = partner._caller_fields()

		doc, _a = partner._update_one(
			lead.name, frappe._dict({"custom_current_program": first}),
			mp, False, pf, ca, allowed_programs=programs)
		self.assertEqual(doc.custom_current_program, first, "an update must transition the program")

		doc, _a = partner._update_one(
			lead.name, frappe._dict({"custom_current_program": second}),
			mp, False, pf, ca, allowed_programs=programs)
		self.assertEqual(doc.custom_current_program, second, "a second transition must also land")

	def test_an_update_refuses_a_program_the_key_is_not_permitted(self):
		"""The update path validates through the same resolver the create does -- it does not blindly
		write whatever was sent."""
		lead, _ = self._lead("+919812300106")
		programs = frappe.get_all("CRM Program", pluck="name", order_by="name", limit=2)
		if not programs:
			self.skipTest("no CRM Programs configured")

		mp = frappe._dict({"source": None, "vertical": VERTICAL, "crm_group": GROUP, "program": None})
		_u, _m, _s, pf, ca = partner._caller_fields()

		with self.assertRaises(frappe.ValidationError) as ctx:
			partner._update_one(
				lead.name, frappe._dict({"custom_current_program": "__not_permitted__"}),
				mp, False, pf, ca, allowed_programs=programs)
		# The BEHAVIOUR, not the prose: the refusal classifies as a 400 validation_error and names the
		# field it is about. Matching a substring of the sentence is the coupling that made rewording a
		# message break a test that was never about the wording.
		code, http, _message, fields, _detail = _base._classify(ctx.exception, "lead_update")
		self.assertEqual((code, http), ("validation_error", 400))
		self.assertEqual(fields, ["custom_current_program"],
		                 "the refusal must name the field in error.fields")

	# -- P3: the label is written the same way on every entity ---------------

	def test_an_overlong_external_id_is_the_same_clean_400_on_every_entity(self):
		"""One input, one answer. The label was written with db.set_value AFTER the insert on three of
		the four entities, bypassing Frappe's length check -- so an over-long label raised a raw
		MariaDB DataError, which _classify does not map, giving an opaque 500 on a record that had
		already been committed. The file path answered the same input with a clean 400."""
		lead, _ = self._lead("+919812300095")
		task = self._task(lead.name)
		long_label = "X" * 600  # the column is Data(500) on all four

		cases = (
			("lead", lambda: self._lead("+919812300096", external_id=long_label)),
			("call", lambda: self._call(lead.name, external_id=long_label)),
			("file", lambda: self._attach(lead.name, external_id=long_label)),
			("activity", lambda: partner_activity._create_one(
				frappe._dict({"lead": lead.name, "task_type": task.custom_task_type or "x",
				              "external_id": long_label, "values": {}}), self.mp, self.is_sysmgr)),
		)
		for entity, run in cases:
			with self.subTest(entity=entity):
				with self.assertRaises(frappe.ValidationError) as ctx:
					run()
				self.assertIn(
					"external_id", str(ctx.exception),
					f"{entity}: an over-long label must name the field it rejected, not 500",
				)

	def test_the_label_guard_runs_before_anything_is_written(self):
		"""The guard is the first thing past validation, so a rejected label leaves no row behind."""
		before = frappe.db.count("CRM Call Log")
		lead, _ = self._lead("+919812300097")
		with self.assertRaises(frappe.ValidationError):
			self._call(lead.name, external_id="Y" * 600)
		self.assertEqual(
			frappe.db.count("CRM Call Log"), before,
			"a rejected label must not leave a call log behind",
		)

	# -- P5: one preamble. Authorize, then charge, then dedupe the retry -----

	def test_the_gate_runs_before_the_idempotency_replay(self):
		"""THE KILL SWITCH. The replay used to short-circuit ABOVE the gate, so a partner whose mapping
		had been disabled still received the stored 200 for every key they had already used -- for as
		long as the row lived, which with the dormant purge job is forever."""
		order = []
		with patch.object(_base, "_load_caller",
		                  side_effect=lambda: order.append("gate") or ("u", None, True)), \
		     patch.object(_base, "_idem_key", return_value="K"), \
		     patch.object(_base, "_idempotency_begin",
		                  side_effect=lambda u, k, f: order.append("idem") or ("run", None)):

			@_api
			def probe(**_kwargs):
				order.append("endpoint")

			frappe.local.response = frappe._dict()
			frappe.form_dict = frappe._dict()
			probe()

		self.assertEqual(
			order, ["gate", "idem", "endpoint"],
			"the authorization gate must run BEFORE the idempotency claim, not after it",
		)

	def test_a_revoked_partner_is_refused_even_on_a_replayed_key(self):
		"""The gate is fail-closed: a 403 beats a stored response."""
		with patch.object(_base, "_load_caller",
		                  side_effect=frappe.PermissionError("mapping disabled")), \
		     patch.object(_base, "_idem_key", return_value="ALREADY-USED"), \
		     patch.object(_base, "_idempotency_begin") as begin:

			@_api
			def probe(**_kwargs):
				self.fail("a revoked partner must never reach the endpoint")

			frappe.local.response = frappe._dict()
			frappe.form_dict = frappe._dict()
			probe()

		self.assertEqual(frappe.local.response["error"]["code"], "forbidden")
		begin.assert_not_called()  # the key is never even claimed

	def test_the_gate_costs_one_db_read_per_request(self):
		"""_load_caller is the gate's only DB read. It used to run up to three times per request -- the
		wrapper, the endpoint body and _meter_volume each resolved the caller independently."""
		lead, _ = self._lead("+919812300098")
		real, loads = _base._load_caller, []

		with patch.object(_base, "_load_caller", side_effect=lambda: loads.append(1) or real()):
			frappe.local.response = frappe._dict()
			frappe.form_dict = frappe._dict({"lead": lead.name})
			partner_call.call_list()

		self.assertEqual(
			len(loads), 1,
			f"the gate hit the DB {len(loads)} times in one request; the preamble must resolve it once",
		)

	# -- P7: a bulk write cannot silently no-op -----------------------------

	def test_a_mistyped_bulk_body_key_is_a_400_not_a_green_200(self):
		"""`{"lead": [...]}` for `{"leads": [...]}` used to answer 200 {total:0} on every bulk WRITE --
		nothing written, no error -- while every bulk READ correctly 400'd. Both lanes now agree."""
		writes = (
			(partner.lead_create_bulk, "leads"),
			(partner.lead_update_bulk, "updates"),
			(partner.lead_delete_bulk, "names"),
			(partner_activity.activity_create_bulk, "activities"),
			(partner_activity.activity_update_bulk, "updates"),
			(partner_activity.activity_delete_bulk, "names"),
			(partner_file.file_attach_bulk, "files"),
			(partner_file.file_delete_bulk, "names"),
			(partner_call.call_create_bulk, "calls"),
			(partner_call.call_update_bulk, "updates"),
			(partner_call.call_delete_bulk, "names"),
		)
		for endpoint, key in writes:
			with self.subTest(endpoint=endpoint.__name__):
				frappe.local.response = frappe._dict()
				frappe.form_dict = frappe._dict({f"mistyped_{key}": []})
				endpoint()
				self.assertEqual(
					frappe.local.response.get("status"), "error",
					f"{endpoint.__name__} answered a mistyped body key with a SUCCESS envelope",
				)
				self.assertIn(key, frappe.local.response["error"]["message"])

	# -- P9: discovery equals ingestion -------------------------------------

	def test_the_list_filter_speaks_the_same_vocabulary_as_the_create(self):
		"""Filtering by the exact task_type the create just accepted must find it. activity_list tested
		membership in a set of composite grain PKs and, on a miss, substituted a sentinel matching
		nothing -- so the filter returned a successful 200 with total: 0, and a typo looked identical."""
		lead, _ = self._lead("+919812300099")
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict({"lead": lead.name})
		partner_activity.activity_schema()
		types = frappe.local.response["data"]["task_types"]
		if not types:
			self.skipTest("no activity type is available for this lead's grain")

		# the human name the schema advertises -- exactly what a partner would send
		advertised = types[0]["name"]
		created = partner_activity._create_one(
			frappe._dict({"lead": lead.name, "task_type": advertised, "values": {}}),
			self.mp, self.is_sysmgr)

		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict({"lead": lead.name, "task_type": advertised})
		partner_activity.activity_list()
		found = {a["name"] for a in frappe.local.response["data"]["activities"]}
		self.assertIn(
			created["name"], found,
			f"activity_list?task_type={advertised!r} did not find the activity activity_create had "
			f"just accepted under that very name",
		)

	def test_an_unavailable_task_type_is_refused_by_the_filter_not_silently_empty(self):
		"""A typo must be a refusal, not a successful empty page. (@_api swallows the throw into the
		error envelope, so the body is what a partner actually sees.)"""
		lead, _ = self._lead("+919812300100")
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict({"lead": lead.name, "task_type": "No Such Type"})
		partner_activity.activity_list()

		self.assertEqual(
			frappe.local.response.get("status"), "error",
			"an unavailable task_type returned a SUCCESSFUL page -- a typo is indistinguishable "
			"from an empty result",
		)
		self.assertEqual(frappe.local.response["error"]["code"], "validation_error")

	# -- P12: the error names the right entity -------------------------------

	def test_cannot_delete_does_not_name_the_wrong_entity(self):
		"""_classify is the one error brain for all four entities; its LinkExistsError branch said
		"this LEAD cannot be deleted" while deleting an activity, a file or a call."""
		code, http, message, _fields, _detail = _base._classify(frappe.LinkExistsError("x"), "activity_delete")
		self.assertEqual((code, http), ("cannot_delete", 409))
		self.assertNotIn("lead", message.lower(), "the shared message must not name one entity")
		self.assertIn("record", message.lower())

	# -- P2: the DB enforces the dedup rule the API promises ------------------

	def test_the_dedup_rule_is_enforced_by_a_unique_index(self):
		"""P2 was a promise the database did not keep. The lookup in _upsert_one is a NON-LOCKING read,
		so two concurrent creates for one patient both miss and both insert -- and no API path heals it,
		because resolve_lead (behind every activity/file/call endpoint) then picks an arbitrary one."""
		self.assertTrue(
			frappe.db.has_index("tabCRM Lead", "ix_lead_dedup_unique"),
			"CRM Lead has no unique index on (mobile_no, custom_vertical, custom_group) -- the dedup "
			"rule is unenforced and a parallel backfill will silently split a patient across two leads",
		)

	def test_a_racing_insert_folds_onto_the_winner_instead_of_failing_the_caller(self):
		"""When the index fires, the other request has already committed. Fold onto its row: the caller
		gets the same lead either way, which is exactly what the dedup rule promises."""
		phone = "+919812300102"
		# This race exercises the TWO pre-insert reads the blind below is calibrated for: _upsert_one's
		# own dedup lookup AND dedup_guard's validate-time lookup. dedup_guard only reads when the dedup
		# automation is enabled, which is dormant-OFF by default — so enable it here or only one read
		# happens and the recovery read falls inside the blind. The teardown savepoint restores the default.
		frappe.db.set_value("CRM Tatva Automation", "Lead::CRM Lead::dedup", "enabled", 1)
		first, _ = self._lead(phone)

		# Reproduce the real sequence. A concurrent request has not COMMITTED yet, so neither of the
		# two reads that guard an insert can see it: _upsert_one's own dedup lookup misses, and so does
		# dedup_guard's validate-time lookup (it is the same unguarded read, which is why it races
		# identically and is not a backstop). Both miss, both reach the insert -- and only the UNIQUE
		# INDEX can stop the second one. Once it fires, the winner IS committed, so the recovery lookup
		# must see it: the blind lifts after those two reads.
		real_get_value = frappe.db.get_value
		blinded = {"n": 0}

		def blind_until_the_insert(doctype, filters, *a, **kw):
			if doctype == "CRM Lead" and isinstance(filters, dict) and filters.get("mobile_no") == phone:
				blinded["n"] += 1
				if blinded["n"] <= 2:  # 1 = _upsert_one's dedup read, 2 = dedup_guard's validate read
					return None
			return real_get_value(doctype, filters, *a, **kw)

		with patch.object(frappe.db, "get_value", side_effect=blind_until_the_insert):
			second, action = self._lead(phone)

		self.assertGreaterEqual(blinded["n"], 3, "the insert must actually have been attempted")

		self.assertEqual(action, ACTION_UPDATED, "a racing insert must fold onto the winner, not fail")
		self.assertEqual(second.name, first.name, "the race must converge on ONE lead")
		self.assertEqual(
			frappe.db.count("CRM Lead", {"mobile_no": phone, "custom_vertical": VERTICAL,
			                             "custom_group": GROUP}),
			1, "the race produced two leads for one patient",
		)

	# -- performance: the list is not an N+1 ---------------------------------

	def test_the_activity_list_is_not_an_n_plus_one(self):
		"""_activity_payload re-read every row and rebuilt its type config per row. A 200-row page was
		~1,000 round-trips. The config is now resolved once per DISTINCT type."""
		lead, _ = self._lead("+919812300102")
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict({"lead": lead.name})
		partner_activity.activity_schema()
		types = frappe.local.response["data"]["task_types"]
		if not types:
			self.skipTest("no activity type is available for this lead's grain")

		for _ in range(5):
			partner_activity._create_one(
				frappe._dict({"lead": lead.name, "task_type": types[0]["name"], "values": {}}),
				self.mp, self.is_sysmgr)

		configs = []
		with patch.object(activity_brain, "_type_config",
		                  side_effect=lambda tt: configs.append(tt) or None):
			frappe.local.response = frappe._dict()
			frappe.form_dict = frappe._dict({"lead": lead.name, "limit": 50})
			partner_activity.activity_list()

		self.assertEqual(frappe.local.response["data"]["count"], 5, "all five rows must come back")
		self.assertEqual(
			len(configs), 1,
			f"_type_config ran {len(configs)} times for 5 rows of ONE type; it must run once per "
			f"DISTINCT type, not once per row",
		)

	def test_the_call_log_reference_lookup_is_indexed(self):
		"""call_list filters on (reference_doctype, reference_docname) and full-scanned without this.
		CRM Task already carried the equivalent (ix_refdoc_tasktype_status)."""
		self.assertTrue(
			frappe.db.has_index("tabCRM Call Log", "ix_calllog_reference"),
			"CRM Call Log has no index on its reference pair -- call_list full-scans the table",
		)

	# -- discovery -----------------------------------------------------------

	def test_every_schema_states_identity_and_dedup(self):
		"""Discovery carries the contract, so an integrator never has to infer it."""
		for endpoint, entity in (
			(partner_call.call_schema, "call"),
			(__import__("tatva_connect.api.partner_file", fromlist=["x"]).file_schema, "file"),
		):
			with self.subTest(entity=entity):
				frappe.local.response = frappe._dict()
				frappe.form_dict = frappe._dict()
				endpoint()
				data = frappe.local.response["data"]
				self.assertEqual(data["entity"], entity)
				self.assertEqual(data["identity"]["addressed_by"], "name")
				self.assertIn("does not deduplicate", data["dedup"])
				ext = next(f for f in data["fields"] if f["fieldname"] == "external_id")
				self.assertEqual(ext["behavior"], "OPTIONAL")
				self.assertFalse(ext["required"], "external_id is OPTIONAL on every entity, always")

	def test_every_schema_field_declares_required_ness(self):
		"""No field is ambiguous: each carries an AIP-203 behavior and a boolean required."""
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict()
		partner_call.call_schema()
		fields = frappe.local.response["data"]["fields"]
		self.assertTrue(fields)
		for f in fields:
			with self.subTest(field=f["fieldname"]):
				self.assertIn(f["behavior"], ("REQUIRED", "OPTIONAL", "OUTPUT_ONLY"))
				self.assertIsInstance(f["required"], bool)
		direction = next(f for f in fields if f["fieldname"] == "direction")
		self.assertTrue(direction["required"], "direction is the call's one required field")
		self.assertEqual(direction["allowed_values"], ["Inbound", "Outbound"])
