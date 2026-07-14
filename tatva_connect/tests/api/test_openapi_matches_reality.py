# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE DRIFT LOCK — the code is the authoritative source; the spec must agree with it.

The partner API's OpenAPI spec is what an integrator builds against. When it drifts from the code,
the code keeps working and the partner breaks — and nobody finds out until they call. That is exactly
what happened: the spec documented `lead_schema` returning `data.lead` long after the server had
renamed it to `data.fields`, told partners to read `results[i].name` when the server returns
`results[i].data.name`, and declared the field descriptor as `reqd` when the API emits
`behavior` + `required`. Every one of those breaks a partner on their FIRST call.

This test refuses to let it happen again. It asserts, against the real code:

  * every endpoint the code exposes is in the spec, and vice versa   (no ghosts, no gaps)
  * every documented HTTP verb matches the @frappe.whitelist          (no wrong methods)
  * every endpoint has a response example                             (nothing to guess)
  * every example's SHAPE matches what the endpoint actually returns  (the spec cannot lie)
  * the spec declares authentication                                  (or every generated client 401s)

It drives the endpoints in-process, so it needs no running web server and no network.
"""
import time
import unittest
from pathlib import Path

import frappe

from tatva_connect.api import partner, partner_activity, partner_call, partner_file, partner_note
from tatva_connect.api._base import ERROR_CODES
from tatva_connect.tests.api.spec import load_spec, response_example, spec_paths

MODULES = (partner, partner_activity, partner_call, partner_file, partner_note)

# Endpoints _drive() deliberately does not exercise, each with the reason. EMPTY, and verified empty:
# all 48 are driven. An entry here buys silence for one endpoint, so it is a decision, never a default.
UNDRIVEN = frozenset()
VERTICAL, GROUP = "GoodFlip Care", "Anaya"


def _code_endpoints():
	"""{dotted_path: (fn, verbs)} for every @_api endpoint the code exposes."""
	out = {}
	for module in MODULES:
		for name in dir(module):
			fn = getattr(module, name)
			if getattr(fn, "_partner_lane", None) is None:
				continue
			verbs = frappe.allowed_http_methods_for_whitelisted_func.get(fn)
			out[f"{module.__name__}.{name}"] = (fn, verbs)
	return out


def _shape(value, depth=0):
	"""The key-shape of a payload, ignoring values. A dict -> its sorted keys plus each child's shape;
	a list -> the shape of its first element. This is what an integrator codes against."""
	if depth > 4:
		return "..."
	if isinstance(value, dict):
		return {k: _shape(v, depth + 1) for k, v in sorted(value.items())}
	if isinstance(value, list):
		return [_shape(value[0], depth + 1)] if value else []
	return type(value).__name__


def _missing_keys(example, actual, path=""):
	"""Keys the EXAMPLE promises that the ACTUAL response does not have. A partner copies the example,
	so a key here is a field they will read as undefined. The reverse (extra keys in the response) is
	merely undocumented, not a lie, so it is not failed on."""
	bad = []
	if isinstance(example, dict) and isinstance(actual, dict):
		for k, v in example.items():
			p = f"{path}.{k}" if path else k
			if k not in actual:
				bad.append(p)
			else:
				bad += _missing_keys(v, actual[k], p)
	elif isinstance(example, list) and isinstance(actual, list):
		if example and actual:
			bad += _missing_keys(example[0], actual[0], f"{path}[]")
	return bad


PARTNER = "spec.lock.partner@example.test"


class TestOpenApiMatchesReality(unittest.TestCase):
	"""Drives the API AS A PARTNER, because that is who the docs are for.

	A trusted System Manager sees a different `lead_schema.routing` block (caller-supplied routing,
	the full catalog) than a partner does (routing forced from their mapping). Both are correct, so an
	example captured as one and checked against the other diverges for no real reason. The lock mints
	its own partner -- user, marker role and grain mapping -- so it is portable and self-contained."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.spec = load_spec()
		cls.code = _code_endpoints()
		cls._mint_partner()
		frappe.set_user(PARTNER)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for dt, name in (("CRM Lead API Mapping", PARTNER), ("User", PARTNER)):
			if frappe.db.exists(dt, name):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def _mint_partner(cls):
		"""A partner is a role-less System User carrying the `Partner API User` marker role, plus ONE
		enabled CRM Lead API Mapping pinning their grain. That single row is the whole enablement gate."""
		if not frappe.db.exists("User", PARTNER):
			frappe.get_doc({
				"doctype": "User", "email": PARTNER, "first_name": "Spec Lock",
				"send_welcome_email": 0, "user_type": "System User",
			}).insert(ignore_permissions=True)
		user = frappe.get_doc("User", PARTNER)
		if "Partner API User" not in [r.role for r in user.roles]:
			user.append("roles", {"role": "Partner API User"})
			user.save(ignore_permissions=True)
		if not frappe.db.exists("CRM Lead API Mapping", PARTNER):
			frappe.get_doc({
				"doctype": "CRM Lead API Mapping", "partner_user": PARTNER, "enabled": 1,
				"vertical": VERTICAL, "crm_group": GROUP,
			}).insert(ignore_permissions=True)
		frappe.db.commit()  # the drive rolls back, and the gate must survive that

	def test_the_spec_documents_exactly_the_endpoints_the_code_exposes(self):
		"""No ghost endpoints in the docs, no undocumented endpoints in the code."""
		documented = {p.rsplit("/", 1)[-1] for p in spec_paths(self.spec)}
		documented.discard("frappe.auth.get_logged_user")
		in_code = set(self.code)

		self.assertFalse(
			sorted(in_code - documented),
			f"endpoints the code exposes but the spec does not document: {sorted(in_code - documented)}",
		)
		self.assertFalse(
			sorted(documented - in_code),
			f"endpoints the spec documents that the code does not expose: {sorted(documented - in_code)}",
		)

	def test_every_documented_verb_matches_the_whitelist(self):
		"""A spec that says POST where the code says PUT sends every integrator into a 405."""
		for dotted, (_fn, verbs) in self.code.items():
			with self.subTest(endpoint=dotted):
				path = self.spec["paths"].get(f"/api/method/{dotted}")
				self.assertTrue(path, f"{dotted} is not in the spec")
				documented = [v.upper() for v in path if v.lower() in
				              ("get", "post", "put", "delete", "patch")]
				self.assertEqual(
					sorted(documented), sorted(verbs),
					f"{dotted}: the spec documents {documented}, the code whitelists {verbs}",
				)

	def test_every_endpoint_that_can_succeed_has_a_response_example(self):
		"""An endpoint with no example leaves an integrator guessing the shape.

		An endpoint that CANNOT succeed against real data is reported, not skipped silently:
		activity_delete is one -- every task type on the seeded grain is is_logged_complete, so every
		activity is immediately linked to an audit record and the delete is refused with 409. That is
		fail-closed by design (a partner may not erase an audit trail), and it is the truth the spec
		must tell."""
		driven = self._drive()
		missing, unexercisable = [], []
		for dotted in self.code:
			succeeded = driven.get(dotted, {}).get("status") == "success"
			has_example = response_example(self.spec, f"/api/method/{dotted}") is not None
			if succeeded and not has_example:
				missing.append(dotted)
			elif not succeeded:
				unexercisable.append(f"{dotted} -> {driven.get(dotted, {}).get('error', {}).get('code', '?')}")

		# An endpoint _drive() never exercises is checked by NOTHING: not this test, and not the lie
		# detector, which iterates the driven map. A whole resource was once added to MODULES without
		# being driven, and every one of its examples was wrong while the lock stayed green. Silence is
		# not a pass, so an undriven endpoint fails here unless it is named in UNDRIVEN with a reason.
		undriven = sorted(d for d in self.code if d not in driven and d not in UNDRIVEN)
		self.assertFalse(
			undriven,
			f"{len(undriven)} endpoint(s) are never driven by _drive(), so their examples are checked by "
			f"nothing and could say anything. Drive them, or add them to UNDRIVEN with the reason: {undriven}",
		)

		if unexercisable:
			print("\n  endpoints that cannot succeed against the seeded data (documented, not skipped):")
			for u in unexercisable:
				print(f"    {u}")
		self.assertFalse(missing, f"{len(missing)} endpoints succeed but have no example: {missing}")

	def test_the_error_vocabulary_is_published_wherever_a_caller_looks_it_up(self):
		"""Every code the API can emit must be findable — in the spec's enum AND on the errors page.

		A caller branches on `error.code`, so a code the API emits and the docs do not list is a lie by
		omission. The vocabulary lived in three places and drifted: `conflict`, returned by every
		Idempotency-Key collision, was emitted for months and published in neither. `_base.ERROR_CODES`
		is now the one declaration, and this locks the other two to it."""
		spec = load_spec()
		enum = set(spec["components"]["schemas"]["PartnerError"]
		           ["properties"]["error"]["properties"]["code"]["enum"])

		page = Path(__file__).resolve().parents[3] / "api-docs" / "pages" / "partner-errors.mdx"
		documented = {line.split("`")[1] for line in page.read_text().splitlines()
		              if line.startswith("| `")}

		self.assertEqual(
			ERROR_CODES - enum, set(),
			"a code the API can emit is missing from the OpenAPI enum — a generated client cannot "
			"branch on it",
		)
		self.assertEqual(
			ERROR_CODES - documented, set(),
			"a code the API can emit is missing from the errors page — a partner meets it with "
			"nothing to look up",
		)
		self.assertEqual(
			enum - ERROR_CODES, set(),
			"the spec advertises a code the API can never emit",
		)

	def test_the_spec_declares_authentication(self):
		"""Without securitySchemes, every generated client -- Postman, PyCharm, codegen -- emits
		requests with no Authorization header and gets a 401."""
		schemes = self.spec.get("components", {}).get("securitySchemes") or {}
		self.assertTrue(schemes, "the spec declares no securitySchemes")
		self.assertTrue(self.spec.get("security"), "the spec declares no root security requirement")
		scheme = next(iter(schemes.values()))
		self.assertEqual(scheme["in"], "header")
		self.assertEqual(scheme["name"], "Authorization")

	def test_no_example_promises_a_field_the_api_never_sends(self):
		"""THE LIE DETECTOR. Drive every endpoint for real and compare its response to the example the
		spec shows. A key the example has and the response does not is a field a partner will read as
		undefined -- which is how `data.lead`, `results[i].name` and `fields[].reqd` shipped."""
		lies = {}
		for dotted, actual in self._drive().items():
			example = response_example(self.spec, f"/api/method/{dotted}")
			if example is None or not isinstance(actual, dict) or actual.get("status") != "success":
				continue
			bad = _missing_keys(example, actual)
			if bad:
				lies[dotted] = bad

		self.assertFalse(
			lies,
			"the spec promises fields the API does not send:\n  " +
			"\n  ".join(f"{k}: {v}" for k, v in lies.items()),
		)

	# -- drive every endpoint in-process ------------------------------------

	def _drive(self):
		"""{dotted: response body} for every endpoint, run against a real lead in a savepoint."""
		# No savepoint: @_api rolls the WHOLE transaction back when an endpoint throws (a 409 from a
		# delete does exactly that), which would destroy it. The final rollback below is what cleans up,
		# and the deletes are driven last so a legitimate 409 cannot take the rest of the run with it.
		form = frappe.form_dict
		out = {}

		def hit(fn, dotted, **args):
			"""Drive one endpoint, honouring a throttle the way the docs tell a partner to.

			Bulk has its own bucket and a capacity of ONE, so driving the twelve bulk endpoints
			back to back is exactly the traffic that bucket exists to refuse. A 429 here is the API
			working, not failing; the spec is documented from the response that comes back AFTER the
			retry, which is the response a partner actually sees."""
			for _attempt in range(6):
				frappe.local.response = frappe._dict()
				frappe.form_dict = frappe._dict(args)
				fn()
				body = dict(frappe.local.response)
				if (body.get("error") or {}).get("code") != "rate_limited":
					out[dotted] = body
					return body
				time.sleep((body["error"].get("retry_after") or 1) + 0.2)
			raise AssertionError(f"{dotted} stayed rate-limited across every retry")

		try:
			r = hit(partner.lead_create, "tatva_connect.api.partner.lead_create",
			        mobile_no="+919812399001", first_name="SpecLock",
			        custom_vertical=VERTICAL, custom_group=GROUP, external_id="SPEC-1")
			lead = r["data"]["name"]

			hit(partner.lead_schema, "tatva_connect.api.partner.lead_schema")
			hit(partner.lead_get, "tatva_connect.api.partner.lead_get", name=lead)
			hit(partner.lead_update, "tatva_connect.api.partner.lead_update", name=lead, first_name="SpecLock2")
			hit(partner.lead_list, "tatva_connect.api.partner.lead_list", limit=5)
			r = hit(partner.lead_create_bulk, "tatva_connect.api.partner.lead_create_bulk",
			        leads=[{"mobile_no": "+919812399002", "first_name": "B1",
			                "custom_vertical": VERTICAL, "custom_group": GROUP}])
			b1 = r["results"][0]["data"]["name"]
			hit(partner.lead_get_bulk, "tatva_connect.api.partner.lead_get_bulk", names=[b1])
			hit(partner.lead_update_bulk, "tatva_connect.api.partner.lead_update_bulk",
			    updates=[{"name": b1, "first_name": "B1x"}])
			hit(partner.lead_delete_bulk, "tatva_connect.api.partner.lead_delete_bulk", names=[b1])

			r = hit(partner_activity.activity_schema,
			        "tatva_connect.api.partner_activity.activity_schema", lead=lead)
			types = r["data"]["task_types"]
			if types:
				tt = types[0]["name"]
				r = hit(partner_activity.activity_create,
				        "tatva_connect.api.partner_activity.activity_create",
				        lead=lead, task_type=tt, values={}, external_id="SPEC-A")
				act = r["data"]["name"]
				hit(partner_activity.activity_get,
				    "tatva_connect.api.partner_activity.activity_get", name=act)
				hit(partner_activity.activity_update,
				    "tatva_connect.api.partner_activity.activity_update", name=act, task_type=tt, values={})
				hit(partner_activity.activity_list,
				    "tatva_connect.api.partner_activity.activity_list", lead=lead, limit=10)
				r = hit(partner_activity.activity_create_bulk,
				        "tatva_connect.api.partner_activity.activity_create_bulk",
				        activities=[{"lead": lead, "task_type": tt, "values": {}}])
				a1 = r["results"][0]["data"]["name"]
				hit(partner_activity.activity_get_bulk,
				    "tatva_connect.api.partner_activity.activity_get_bulk", names=[a1])
				hit(partner_activity.activity_update_bulk,
				    "tatva_connect.api.partner_activity.activity_update_bulk",
				    updates=[{"name": a1, "task_type": tt, "values": {}}])
				hit(partner_activity.activity_delete_bulk,
				    "tatva_connect.api.partner_activity.activity_delete_bulk", names=[a1])

			hit(partner_call.call_schema, "tatva_connect.api.partner_call.call_schema")
			r = hit(partner_call.call_create, "tatva_connect.api.partner_call.call_create",
			        lead=lead, direction="Inbound", from_number="9812399001", to_number="9000000000")
			cal = r["data"]["name"]
			hit(partner_call.call_get, "tatva_connect.api.partner_call.call_get", name=cal)
			hit(partner_call.call_update, "tatva_connect.api.partner_call.call_update",
			    name=cal, status="Completed", duration=42)
			hit(partner_call.call_list, "tatva_connect.api.partner_call.call_list", lead=lead, limit=10)
			r = hit(partner_call.call_create_bulk, "tatva_connect.api.partner_call.call_create_bulk",
			        calls=[{"lead": lead, "direction": "Inbound",
			                "from_number": "9812399001", "to_number": "9000000001"}])
			c1 = r["results"][0]["data"]["name"]
			hit(partner_call.call_get_bulk, "tatva_connect.api.partner_call.call_get_bulk", names=[c1])
			hit(partner_call.call_update_bulk, "tatva_connect.api.partner_call.call_update_bulk",
			    updates=[{"name": c1, "duration": 7}])
			hit(partner_call.call_delete_bulk, "tatva_connect.api.partner_call.call_delete_bulk", names=[c1])
			hit(partner_call.call_delete, "tatva_connect.api.partner_call.call_delete", name=cal)

			hit(partner_note.note_schema, "tatva_connect.api.partner_note.note_schema")
			r = hit(partner_note.note_create, "tatva_connect.api.partner_note.note_create",
			        lead=lead, content="<p>spec</p>")
			nt = r["data"]["name"]
			hit(partner_note.note_get, "tatva_connect.api.partner_note.note_get", name=nt)
			hit(partner_note.note_update, "tatva_connect.api.partner_note.note_update",
			    name=nt, content="<p>spec revised</p>")
			hit(partner_note.note_list, "tatva_connect.api.partner_note.note_list", lead=lead, limit=10)
			r = hit(partner_note.note_create_bulk, "tatva_connect.api.partner_note.note_create_bulk",
			        notes=[{"lead": lead, "content": "<p>bulk</p>"}])
			n1 = r["results"][0]["data"]["name"]
			hit(partner_note.note_get_bulk, "tatva_connect.api.partner_note.note_get_bulk", names=[n1])
			hit(partner_note.note_update_bulk, "tatva_connect.api.partner_note.note_update_bulk",
			    updates=[{"name": n1, "content": "<p>bulk revised</p>"}])
			hit(partner_note.note_delete_bulk, "tatva_connect.api.partner_note.note_delete_bulk", names=[n1])
			hit(partner_note.note_delete, "tatva_connect.api.partner_note.note_delete", name=nt)

			hit(partner_file.file_schema, "tatva_connect.api.partner_file.file_schema")
			r = hit(partner_file.file_attach, "tatva_connect.api.partner_file.file_attach",
			        lead=lead, filename="spec.txt", file_type="Prescription", content_base64="cw==")
			fil = r["data"]["name"]
			hit(partner_file.file_get, "tatva_connect.api.partner_file.file_get", name=fil)
			hit(partner_file.file_list, "tatva_connect.api.partner_file.file_list", lead=lead, limit=10)
			r = hit(partner_file.file_attach_bulk, "tatva_connect.api.partner_file.file_attach_bulk",
			        files=[{"lead": lead, "filename": "spec2.txt", "content_base64": "cw=="}])
			f1 = r["results"][0]["data"]["name"]
			hit(partner_file.file_get_bulk, "tatva_connect.api.partner_file.file_get_bulk", names=[f1])
			hit(partner_file.file_delete_bulk, "tatva_connect.api.partner_file.file_delete_bulk", names=[f1])
			hit(partner_file.file_delete, "tatva_connect.api.partner_file.file_delete", name=fil)

			# A lead carrying a linked activity cannot be deleted, and neither can the activity (every
			# task type on this grain is is_logged_complete, so an audit record links to it). Both
			# refusals are correct. Drive lead_delete against a CHILDLESS lead so a successful delete
			# is documented, then let the linked pair produce their real 409s -- last, because @_api
			# rolls the whole transaction back on a throw.
			r = hit(partner.lead_create, "tatva_connect.api.partner.lead_create",
			        mobile_no="+919812399009", first_name="SpecLockDeletable")
			hit(partner.lead_delete, "tatva_connect.api.partner.lead_delete", name=r["data"]["name"])
			if types:
				hit(partner_activity.activity_delete,
				    "tatva_connect.api.partner_activity.activity_delete", name=act)
		finally:
			frappe.form_dict = form
			frappe.db.rollback()
		return out
