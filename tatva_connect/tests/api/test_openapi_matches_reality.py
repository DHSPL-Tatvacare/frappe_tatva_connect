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
import json
import re
import time
import unittest
from pathlib import Path

import frappe

from tatva_connect.api import (
	partner,
	partner_activity,
	partner_bulk_job,
	partner_call,
	partner_deal,
	partner_file,
	partner_note,
)
from tatva_connect.api._base import _RATE_ENFORCEMENT, DEFAULTS, ERROR_CODES
from tatva_connect.tests.api.partner_fixture import minimal_answers
from tatva_connect.tests.api.spec import load_spec, response_example, spec_paths

MODULES = (partner, partner_activity, partner_call, partner_deal, partner_file, partner_note,
           partner_bulk_job)

# Endpoints _drive() deliberately does not exercise, each with the reason. EMPTY, and verified empty:
# every one is driven. An entry here buys silence for one endpoint, so it is a decision, never a default.
UNDRIVEN = frozenset()
VERTICAL, GROUP = "Goodflip-Care", "Anaya"


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
		frappe.db.set_value("CRM Tatva Automation", "Partner::AsyncBulk::jobs", "enabled", 1)  # drive the async tier
		frappe.db.commit()
		frappe.set_user(PARTNER)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.db.set_value("CRM Tatva Automation", "Partner::AsyncBulk::jobs", "enabled", 0)  # back to dormant
		for contract in frappe.get_all("CRM Lead API Mapping", filters={"partner_user": PARTNER}, pluck="name"):
			frappe.delete_doc("CRM Lead API Mapping", contract, force=True, ignore_permissions=True)
		if frappe.db.exists("User", PARTNER):
			frappe.delete_doc("User", PARTNER, force=True, ignore_permissions=True)
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
		# Found by the partner_user COLUMN: the contract's name is its grain composite, not the login.
		if not frappe.db.exists("CRM Lead API Mapping", {"partner_user": PARTNER}):
			frappe.get_doc({
				"doctype": "CRM Lead API Mapping", "partner_user": PARTNER, "enabled": 1,
				"contract_name": PARTNER, "vertical": VERTICAL, "crm_group": GROUP,
			}).insert(ignore_permissions=True)
		frappe.db.commit()  # the drive rolls back, and the gate must survive that

	def test_the_spec_documents_exactly_the_endpoints_the_code_exposes(self):
		"""No ghost endpoints in the docs, no undocumented endpoints in the code."""
		# No carve-out. A framework method (frappe.auth.get_logged_user) was documented here as the
		# key check; it is not a partner surface, it answers off a browser session when the key header
		# is absent, and it was exempted from this very assertion. `lead_schema` verifies a key now.
		documented = {p.rsplit("/", 1)[-1] for p in spec_paths(self.spec)}
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

	def test_no_docs_page_shows_a_response_key_the_api_never_sends(self):
		"""THE LIE DETECTOR, pointed at the PAGES. The OpenAPI spec has been driven against reality
		since this lock was written; the MDX pages beside it never were, and they drifted exactly where
		nothing watched. Three of them showed a write returning
		`data: {name, source, vertical, group, program}` long after the server had settled on the field
		names -- `custom_vertical`, `custom_group`, `custom_current_program` -- and on returning the
		whole record. An integrator reading `data.vertical` off the Quickstart got undefined.

		Every ```json block on a page whose top level is the success envelope is parsed, and each key
		under `data` is checked against the union of keys the driven endpoints really return. A key that
		appears in NO response is a key a partner cannot read.

		A block that will not parse is not silently passed over: those are counted, and the count of
		blocks actually CHECKED has a floor, so this test cannot quietly degrade to checking nothing."""
		pages_dir = Path(__file__).resolve().parents[3] / "api-docs" / "pages"
		self.assertTrue(pages_dir.is_dir(), f"the docs pages are not where this expects: {pages_dir}")

		driven = self._drive()
		real, succeeded = set(), 0
		for body in driven.values():
			if not (isinstance(body, dict) and body.get("status") == "success"):
				continue
			succeeded += 1
			if isinstance(body.get("data"), dict):
				real |= set(body["data"])
			for row in body.get("results") or []:
				if isinstance(row, dict) and isinstance(row.get("data"), dict):
					real |= set(row["data"])

		# The union is only an oracle while the drive is broad. A page block naming a key that only
		# `lead_list` returns is a LIE when lead_list was driven and did not return it, and a false
		# accusation when lead_list never ran at all. So an incomplete drive is reported as an
		# incomplete drive, and this test refuses to judge the pages on it.
		self.assertGreaterEqual(
			succeeded, 30,
			f"only {succeeded} endpoints were driven successfully, so the set of keys the API really "
			f"returns is incomplete and the pages cannot be judged against it. Failures: " +
			str({d: (b.get("error") or {}).get("code") for d, b in driven.items()
			     if b.get("status") != "success"}),
		)

		checked, unparsed, lies = 0, [], {}
		for page in sorted(pages_dir.glob("*.mdx")):
			for block in re.findall(r"```json\n(.*?)```", page.read_text(), re.S):
				# `// HTTP 200` and `/* ... */` annotate these blocks for the reader; they are not JSON.
				cleaned = re.sub(r"/\*.*?\*/", "null", re.sub(r"^\s*//.*$", "", block, flags=re.M), flags=re.S)
				try:
					doc = json.loads(cleaned)
				except json.JSONDecodeError:
					unparsed.append(f"{page.name}: {block.strip().splitlines()[0][:48]}")
					continue
				if not (isinstance(doc, dict) and doc.get("status") == "success"
				        and isinstance(doc.get("data"), dict)):
					continue
				checked += 1
				unknown = sorted(k for k in doc["data"] if k not in real)
				if unknown:
					lies.setdefault(page.name, []).extend(unknown)

		self.assertFalse(
			lies,
			"a docs page shows a response key the API never sends:\n  " +
			"\n  ".join(f"{k}: {v}" for k, v in lies.items()) +
			f"\n(keys the driven endpoints actually return: {sorted(real)})",
		)
		# Silence is not a pass. If a refactor stops these blocks parsing, the assertion above passes
		# vacuously; this floor is what makes that show up as a failure instead.
		self.assertGreaterEqual(
			checked, 6,
			f"only {checked} response block(s) on the docs pages could be checked "
			f"({len(unparsed)} did not parse: {unparsed}) -- this test has stopped testing anything",
		)

	def test_the_documented_headers_are_the_headers_a_partner_receives(self):
		"""The header block was prose NOTHING drove, and it rotted exactly as you would expect: it
		promised a Reset that counted down while the code sent a constant window, and showed a Remaining
		of 1158 beside a Limit of 120. Enforcement ships dormant, so turn it on -- the way UAT runs it --
		and hold the wire to the spec."""
		documented = set(self.spec["components"]["headers"])
		frappe.set_user("Administrator")
		# Restored to what it WAS, not to dormant: this switch is operator policy, and a test that
		# hardcodes the default silently disarms a bench where someone had deliberately turned it on.
		was = frappe.db.get_value("CRM Tatva Automation", _RATE_ENFORCEMENT, "enabled")
		frappe.db.set_value("CRM Tatva Automation", _RATE_ENFORCEMENT, "enabled", 1)
		frappe.db.commit()
		try:
			frappe.set_user(PARTNER)
			frappe.local.response = frappe._dict()
			frappe.local.response_headers = frappe._dict()
			frappe.form_dict = frappe._dict()
			partner.lead_list()
			emitted = dict(frappe.local.response_headers)
		finally:
			frappe.set_user("Administrator")
			frappe.db.set_value("CRM Tatva Automation", _RATE_ENFORCEMENT, "enabled", was)
			frappe.db.commit()
			frappe.set_user(PARTNER)

		self.assertTrue(emitted, "enforcement is on, so a partner call must report its budget")
		self.assertFalse(
			set(emitted) - documented,
			f"headers on the wire that the spec never mentions: {sorted(set(emitted) - documented)}",
		)
		limit, remaining = int(emitted["X-RateLimit-Limit"]), int(emitted["X-RateLimit-Remaining"])
		self.assertLessEqual(remaining, limit, "Remaining must never exceed the Limit it is measured against")
		self.assertLessEqual(
			int(emitted["X-RateLimit-Reset"]), DEFAULTS["window_seconds"],
			"Reset is the wait for a full budget, so it can never outrun one window",
		)
		self.assertFalse(
			[h for h in emitted if h.startswith("RateLimit-")],
			"the superseded bare RateLimit-* spelling is back on the wire and is not in the spec",
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
				# A type whose rules ask for an answer refuses an empty form, so the example is driven
				# with the smallest submission that type accepts, computed from its own schema.
				answers = minimal_answers(tt)
				r = hit(partner_activity.activity_create,
				        "tatva_connect.api.partner_activity.activity_create",
				        lead=lead, task_type=tt, values=answers, external_id="SPEC-A")
				act = r["data"]["name"]
				hit(partner_activity.activity_get,
				    "tatva_connect.api.partner_activity.activity_get", name=act)
				hit(partner_activity.activity_update,
				    "tatva_connect.api.partner_activity.activity_update", name=act, task_type=tt, values=answers)
				hit(partner_activity.activity_list,
				    "tatva_connect.api.partner_activity.activity_list", lead=lead, limit=10)
				r = hit(partner_activity.activity_create_bulk,
				        "tatva_connect.api.partner_activity.activity_create_bulk",
				        activities=[{"lead": lead, "task_type": tt, "values": answers}])
				a1 = r["results"][0]["data"]["name"]
				hit(partner_activity.activity_get_bulk,
				    "tatva_connect.api.partner_activity.activity_get_bulk", names=[a1])
				hit(partner_activity.activity_update_bulk,
				    "tatva_connect.api.partner_activity.activity_update_bulk",
				    updates=[{"name": a1, "task_type": tt, "values": answers}])
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

			# A NOTE-homed file: attached_to_doctype is "FCRM Note" — the branch the lie detector never exercised, which let the spec's enum forbid a real response.
			r = hit(partner_note.note_create, "tatva_connect.api.partner_note.note_create",
			        lead=lead, content="<p>file home</p>")
			note_home = r["data"]["name"]
			r = hit(partner_file.file_attach, "tatva_connect.api.partner_file.file_attach",
			        lead=lead, note=note_home, filename="spec-note.txt", content_base64="cw==")
			hit(partner_file.file_get, "tatva_connect.api.partner_file.file_get", name=r["data"]["name"])

			r = hit(partner_file.file_attach_bulk, "tatva_connect.api.partner_file.file_attach_bulk",
			        files=[{"lead": lead, "filename": "spec2.txt", "content_base64": "cw=="}])
			f1 = r["results"][0]["data"]["name"]
			hit(partner_file.file_get_bulk, "tatva_connect.api.partner_file.file_get_bulk", names=[f1])
			hit(partner_file.file_delete_bulk, "tatva_connect.api.partner_file.file_delete_bulk", names=[f1])
			hit(partner_file.file_delete, "tatva_connect.api.partner_file.file_delete", name=fil)

			# Async bulk-job tier: driven in the transaction (the worker is not run here, so the job stays
			# UploadComplete and the final rollback cleans it up — no committed leads, no worker needed).
			bj = hit(partner_bulk_job.create, "tatva_connect.api.partner_bulk_job.create",
			         operation="lead_create", format="inline",
			         records=[{"mobile_no": "+919812399050", "first_name": "SpecBulk"}])
			bjob = bj["data"]["job_id"]
			hit(partner_bulk_job.get, "tatva_connect.api.partner_bulk_job.get", job_id=bjob)
			hit(partner_bulk_job.results, "tatva_connect.api.partner_bulk_job.results", job_id=bjob)
			hit(partner_bulk_job.cancel, "tatva_connect.api.partner_bulk_job.cancel", job_id=bjob)

			# The deal surface: a deal exists only after conversion, so one is converted here. That is
			# CRM scaffolding, not a partner call -- a partner never creates a deal -- and the rollback
			# below removes it. Without it both deal endpoints answer 404 and their examples, like any
			# endpoint that cannot succeed, would be checked by nothing.
			from crm.fcrm.doctype.crm_lead.crm_lead import convert_to_deal

			ld = frappe.get_doc("CRM Lead", lead)
			ld.flags.ignore_permissions = True
			convert_to_deal(lead=lead, doc=ld)
			hit(partner_deal.deal_get, "tatva_connect.api.partner_deal.deal_get", name=lead)
			hit(partner_deal.deal_update, "tatva_connect.api.partner_deal.deal_update", name=lead)

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
