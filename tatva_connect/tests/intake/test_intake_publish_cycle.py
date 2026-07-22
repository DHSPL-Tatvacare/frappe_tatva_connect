# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The publish cycle — a form an operator publishes is a form a patient can actually open.

Publishing used to be a raw `db.set_value` on the Web Form, which never ran the controller's
`on_update` and so never cleared `get_published_web_forms` (`@redis_cache(ttl=3600)`). The DB said
published, the router's cached route list did not, and the public URL 404'd for up to an hour.

Route reachability is asserted the way frappe asserts its own (`web_form/test_web_form.py`):
`set_request` + `frappe.website.serve.get_response`, in-process against the real router. No curl
(the test transaction is uncommitted, so another process cannot see it) and nothing mocked — the
route really resolves, or it does not.

The route cache is warmed in the UNPUBLISHED state before every publish assertion. That is what the
bug needed to show itself: on a cold cache the first lookup repopulates from the DB and the stale
list never exists, so a test that skips the warm-up passes on the broken code and proves nothing.
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import set_request
from frappe.website.router import clear_routing_cache
from frappe.website.serve import get_response

from tatva_connect import client_scripts_seed
from tatva_connect.automation import seed
from tatva_connect.intake import api, builder, guards, intake
from tatva_connect.intake.guards import _ACCEPT_CMD
from tatva_connect.partner_api import section_seed

_INTAKE_SWITCH = "Lead::Enrolment::intake"
_RATE_SWITCH = "Intake::RateLimit::enforcement"

_VERTICAL = "GoodFlip Care"
_GROUP = "Anaya"
_PROGRAM = "Nivolumab"
_SOURCE = "Enrolment Form"
_FORM = "Publish Cycle Test"

# Deliberately NOT named `phone` — the contract declares which question carries it.
_PHONE_FIELD = "contact_number"

# Three spellings of ONE patient's number; the last is an unquoted JSON number (an int on the wire).
_PHONE_SPELLINGS = ("+91 90000 00011", "9000000011", 9000000011)
_PHONE_CANONICAL = "+919000000011"

# Caps this suite sets for itself and restores — the assertion is the counter KEY, not the limit.
_CAP_FIELDS = ("ip_per_hour", "phone_per_day")


class TestIntakePublishCycle(FrappeTestCase):
	def setUp(self):
		seed.sync_catalog()
		section_seed.ensure_rows()  # the section brain C10 reads `is_key_value` from
		self._made = []
		# Restored in tearDown — a config left behind on the bench has broken a whole suite before.
		self._switch_was = frappe.db.get_value("CRM Tatva Automation", _INTAKE_SWITCH, "enabled")
		self._rate_switch_was = frappe.db.get_value("CRM Tatva Automation", _RATE_SWITCH, "enabled")
		settings = frappe.get_cached_doc("CRM Intake Settings")
		self._caps_was = {f: settings.get(f) for f in _CAP_FIELDS}
		self._set_switch(1)

		self._ensure("CRM Vertical", _VERTICAL, {"vertical_name": _VERTICAL})
		self._ensure("CRM Group", _GROUP, {"group_name": _GROUP})
		self._ensure("CRM Program", _PROGRAM, {"program_name": _PROGRAM})
		self._ensure("CRM Lead Source", _SOURCE, {"source_name": _SOURCE})

		self.cfg = self._intake_form()
		self.dt = builder.doctype_name_for(self.cfg)

	def tearDown(self):
		frappe.set_user("Administrator")
		for wf in frappe.get_all("Web Form", filters={"doc_type": self.dt}, pluck="name"):
			frappe.delete_doc("Web Form", wf, force=True, ignore_permissions=True)
		if frappe.db.exists("DocType", self.dt):
			frappe.delete_doc("DocType", self.dt, force=True, ignore_permissions=True)
			frappe.db.delete("Singles", {"doctype": self.dt})
		for dt, name in reversed(self._made):
			if frappe.db.exists(dt, name):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		intake.bust_intake_doctype_cache()
		# Redis is not rolled back with the transaction — leave no route and no counter behind.
		clear_routing_cache()
		frappe.cache.delete_value(frappe.cache.make_key(f"intake-rl:phone:{_PHONE_CANONICAL}"))
		frappe.cache.delete_value(frappe.cache.make_key("intake-rl:ip:203.0.113.7"))
		self._set_switch(self._switch_was)
		frappe.db.set_value("CRM Tatva Automation", _RATE_SWITCH, "enabled", self._rate_switch_was)
		self._set_caps(self._caps_was)

	# --- helpers ---------------------------------------------------------
	def _set_switch(self, on):
		if frappe.db.exists("CRM Tatva Automation", _INTAKE_SWITCH):
			frappe.db.set_value("CRM Tatva Automation", _INTAKE_SWITCH, "enabled", 1 if on else 0)

	def _ensure(self, doctype, name, values):
		if frappe.db.exists(doctype, name):
			return name
		d = frappe.new_doc(doctype)
		d.update(values)
		d.insert(ignore_permissions=True)
		self._made.append((doctype, d.name))
		return d.name

	def _intake_form(self, mappings=None):
		if frappe.db.exists("CRM Intake Form", _FORM):
			frappe.delete_doc("CRM Intake Form", _FORM, force=True, ignore_permissions=True)
		doc = frappe.get_doc({
			"doctype": "CRM Intake Form",
			"form_name": _FORM,
			"enabled": 1,
			"source": _SOURCE,
			"custom_vertical": _VERTICAL,
			"custom_group": _GROUP,
			"custom_current_program": _PROGRAM,
			"mappings": mappings if mappings is not None else [
				{"source_field": _PHONE_FIELD, "fieldtype": "Phone", "target_table": "lead", "target_field": "mobile_no"},
				{"source_field": "patient_name", "target_table": "lead", "target_field": "first_name"},
			],
		})
		doc.insert(ignore_permissions=True)
		self._made.append(("CRM Intake Form", doc.name))
		frappe.clear_cache(doctype="CRM Intake Form")
		return frappe.get_cached_doc("CRM Intake Form", _FORM)

	def _set_caps(self, caps):
		"""Write the rate caps through the native Single setter, then drop the cached doc the guard
		reads. This bench was found carrying ip_per_hour=2 from an earlier session — a test that
		inherits a cap it did not set fails for reasons that have nothing to do with the code."""
		for field, value in caps.items():
			frappe.db.set_single_value("CRM Intake Settings", field, value)
		frappe.clear_document_cache("CRM Intake Settings", "CRM Intake Settings")

	def _web_form(self):
		"""Locate the Web Form the way the test's own fixture knows it — off the sink doctype, NOT
		through anything this change introduces. An assertion that leans on new code cannot fail for
		the right reason on the old code, and a red that only says "helper missing" proves nothing."""
		return frappe.db.get_value("Web Form", {"doc_type": self.dt}, "name")

	def _is_published(self):
		"""Read the live flag off the Web Form itself — the one source, never a cached echo."""
		wf_name = self._web_form()
		return bool(wf_name and frappe.db.get_value("Web Form", wf_name, "published"))

	def _status(self, path):
		"""Real status code from the real router, the way frappe tests its own web forms."""
		set_request(method="GET", path=path)
		return get_response(path).status_code

	def _warm_route_cache_unpublished(self):
		"""Populate `get_published_web_forms` while this form is NOT in it — the stale list the
		publish must invalidate. Without this the cache is cold and the bug cannot appear."""
		clear_routing_cache()
		self.assertEqual(self._status(f"/{self.cfg.route}/new"), 404, "form must not be reachable before publishing")

	# --- C1 --------------------------------------------------------------
	def test_c1_route_is_reachable_after_publish(self):
		"""The whole feature in one line: Publish, and the patient can open the form.

		Driven through `api.toggle_published` — the method the Desk button actually calls — so this
		asserts the operator's real path, not an internal the button does not use."""
		builder.sync_form(self.cfg)
		self.cfg.reload()
		self._warm_route_cache_unpublished()

		api.toggle_published(self.cfg.name)

		self.assertEqual(
			self._status(f"/{self.cfg.route}/new"), 200,
			"published form must be reachable in the SAME request — the route cache is the bug",
		)

	# --- C2 --------------------------------------------------------------
	def test_c2_the_switch_vetoes_going_live_but_never_a_takedown(self):
		"""Dormant-by-default must be EXPLAINED, never worked around: publishing into a switched-off
		feature scaffolds nothing, so a form that went live would be a shell. Withdrawing is the
		opposite case — a form that is already public must always be stoppable."""
		builder.sync_form(self.cfg)
		self.cfg.reload()

		self._set_switch(0)
		self.cfg.reload()
		with self.assertRaises(frappe.ValidationError):
			api.toggle_published(self.cfg.name)
		self.assertFalse(self._is_published(), "a refused publish must leave the form unpublished")

		self._set_switch(1)
		self.cfg.reload()
		api.toggle_published(self.cfg.name)
		self.assertTrue(self._is_published())

		self._set_switch(0)
		self.cfg.reload()
		api.toggle_published(self.cfg.name)
		self.assertFalse(self._is_published(), "a live form must be withdrawable whatever the switch says")

	# --- C3 --------------------------------------------------------------
	def test_c3_publish_with_no_questions_raises(self):
		"""An empty form collects nothing. The rows are removed directly because the Table field is
		`reqd` — a saved form cannot reach this state through the editor, only by losing its rows."""
		builder.sync_form(self.cfg)
		frappe.db.delete("CRM Intake Field Map", {"parent": self.cfg.name})
		self.cfg.reload()

		with self.assertRaises(frappe.ValidationError):
			api.toggle_published(self.cfg.name)

		self.assertFalse(self._is_published(), "an empty form must not be live")

	# --- C4 --------------------------------------------------------------
	def test_c4_publish_resyncs_so_the_live_form_matches_the_contract(self):
		"""Publishing a STALE scaffold is B3. The contract is edited while the feature switch is off,
		so the save cannot scaffold — exactly how a live form drifts from its contract. Publishing
		must reconcile the two, not take the old form live."""
		builder.sync_form(self.cfg)
		self.cfg.reload()

		self._set_switch(0)
		self.cfg.append("mappings", {"source_field": "surname", "target_table": "lead", "target_field": "last_name"})
		self.cfg.save(ignore_permissions=True)
		self._set_switch(1)
		self.cfg.reload()

		api.toggle_published(self.cfg.name)

		wf = frappe.get_doc("Web Form", self._web_form())
		self.assertTrue(
			any(f.fieldname == "surname" for f in wf.web_form_fields),
			"the published form must carry the question the contract declares",
		)

	# --- C5 --------------------------------------------------------------
	def test_c5_unpublish_withdraws_the_route_in_the_same_request(self):
		"""Taking a form down is as urgent as putting it up — a withdrawn form that keeps serving
		for an hour is the same bug pointed the other way."""
		builder.sync_form(self.cfg)
		self.cfg.reload()

		api.toggle_published(self.cfg.name)
		self.assertEqual(self._status(f"/{self.cfg.route}/new"), 200, "form should be live after publishing")

		api.toggle_published(self.cfg.name)
		self.assertEqual(
			self._status(f"/{self.cfg.route}/new"), 404,
			"unpublished form must stop serving in the SAME request",
		)

	# --- C6 --------------------------------------------------------------
	def test_c6_readiness_is_empty_exactly_when_publish_succeeds(self):
		"""One decision, two readers: what `readiness` reports is precisely what `publish` enforces.
		If these ever disagree, the Desk form is explaining a rule the server does not apply."""
		builder.sync_form(self.cfg)
		self.cfg.reload()

		self.assertEqual(builder.readiness(self.cfg), [], "a complete, enabled form on a live feature is ready")
		api.toggle_published(self.cfg.name)
		self.assertTrue(self._is_published(), "ready means publish succeeds")

		api.toggle_published(self.cfg.name)
		self.assertFalse(self._is_published())

		self._set_switch(0)
		self.cfg.reload()
		self.assertTrue(builder.readiness(self.cfg), "the switch being off must be NAMED as a reason")
		with self.assertRaises(frappe.ValidationError):
			api.toggle_published(self.cfg.name)
		self.assertFalse(self._is_published(), "not ready means publish refuses")

	# --- C7 --------------------------------------------------------------
	def test_c7_phone_throttle_keys_off_the_mapped_question(self):
		"""The per-phone limit must follow the contract's phone question. This form's is
		`contact_number`; a throttle that reads a key literally named `phone` finds nothing and
		silently drops the limit — the form still submits, so nothing looks wrong."""
		builder.sync_form(self.cfg)
		self.cfg.reload()
		intake.bust_intake_doctype_cache()

		frappe.db.set_value("CRM Tatva Automation", _RATE_SWITCH, "enabled", 1)
		# Caps well above the three submissions below: tripping the limit is a DIFFERENT test.
		self._set_caps({"ip_per_hour": 50, "phone_per_day": 50})
		frappe.local.request_ip = "203.0.113.7"

		# The SAME patient submits twice, spelling the number two different ways.
		for spelling in _PHONE_SPELLINGS:
			frappe.form_dict.update({
				"cmd": _ACCEPT_CMD,
				"web_form": self._web_form(),
				"data": frappe.as_json({_PHONE_FIELD: spelling}),
			})
			guards.throttle_intake()

		self.assertEqual(
			int(frappe.cache.get(frappe.cache.make_key(f"intake-rl:phone:{_PHONE_CANONICAL}")) or 0), 3,
			"every spelling of one number must land on ONE canonical counter — a digits-only key "
			"made them separate counters, so the limit was evaded by retyping the number",
		)

	# --- C9 --------------------------------------------------------------
	def test_c9_a_layout_field_cannot_carry_a_target(self):
		"""A Section Break is form furniture and gets no column on the submission table, so a target on
		one lands nothing. It used to save clean and lose the answer in silence."""
		with self.assertRaises(frappe.ValidationError):
			self._intake_form(mappings=[
				{"source_field": _PHONE_FIELD, "fieldtype": "Phone", "target_table": "lead", "target_field": "mobile_no"},
				{"source_field": "a_divider", "fieldtype": "Section Break", "target_table": "lead", "target_field": "first_name"},
			])

	# --- C8 --------------------------------------------------------------
	def test_c8_seeding_raises_on_a_missing_declared_script(self):
		"""A declared script file that isn't there is a rename nobody finished. Skipping it took the
		builder UI off the Desk form while migrate reported success."""
		declared = list(client_scripts_seed.SCRIPTS)
		client_scripts_seed.SCRIPTS[:] = [
			("Intake Missing Script Probe", "CRM Intake Form", "Form", "intake/client_scripts/does_not_exist.js")
		]
		try:
			with self.assertRaises(frappe.ValidationError):
				client_scripts_seed.seed()
		finally:
			client_scripts_seed.SCRIPTS[:] = declared
