# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The public form warns that a number is ALREADY a lead on this form's line — and nothing else.

The fold has always deduped: a submission whose phone matches an existing lead on the same
`mobile_no + custom_vertical + custom_group` anchor MERGES onto it (`api.partner._upsert_one`), and
the person filling the form was never told. This adds the telling. It does NOT change the merge.

Two properties are load-bearing and both are held here:

  * ADVISORY. The check is asked on the phone field's change event and answers a bare yes/no; the
    SUBMIT path is untouched. Every not-a-confident-yes answers False, so a form whose flag is off,
    whose feature switch is off, or whose number is half-typed behaves exactly as it does today.
    (`check_existing_patient` returning False is the only way this feature can fail — never a throw
    the visitor cannot get past. A public form's `frappe.call` is website.js's lightweight one, with
    no error-handler seam, so a submit-time refusal WOULD be a dead end. That is why it is not one.)
  * PER-LINE, LIKE THE ANCHOR IT MIRRORS. It hits on vertical + group and says so; it must NOT hit
    across groups, and it must NOT name a programme — two programmes of one group share ONE lead
    (program is a mutable attribute, never identity), so a programme in the message could be false.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import seed
from tatva_connect.intake import api, builder, intake
from tatva_connect.partner_api import section_seed
from tatva_connect.whatsapp.phone import to_e164

_INTAKE_SWITCH = "Lead::Enrolment::intake"
_DEDUP_SWITCH = "Lead::CRM Lead::dedup"
_RATE_SWITCH = "Intake::RateLimit::enforcement"

_VERTICAL = "Goodflip-Care"
_GROUP = "Anaya"
_OTHER_GROUP = "Zydus"
_PROGRAM = "Nivolumab"
_SIBLING_PROGRAM = "Tukavo"  # same group as _PROGRAM — one lead, not two
_SOURCE = "Enrolment Form"
_FORM = "Duplicate Warning Test Form"
_PHONE = "+91 9000000031"

# Deliberately NOT "phone": which question carries the number is the CONTRACT's to declare, and a
# snippet bound to a field named by convention would work here and silently break on a real form.
_PHONE_QUESTION = "patient_mobile"

_OPERATOR_SCRIPT = "// operator's own script\nfrappe.web_form.events.on('after_load', () => {});"


class TestIntakeDuplicateWarning(FrappeTestCase):
	def setUp(self):
		seed.sync_catalog()
		section_seed.ensure_rows()
		self._made = []
		self._set_switch(_INTAKE_SWITCH, 1)
		self._set_switch(_DEDUP_SWITCH, 1)
		self._set_switch(_RATE_SWITCH, 0)

		self._ensure("CRM Vertical", _VERTICAL, {"vertical_name": _VERTICAL})
		self._ensure("CRM Group", _GROUP, {"group_name": _GROUP})
		self._ensure("CRM Group", _OTHER_GROUP, {"group_name": _OTHER_GROUP})
		self._ensure("CRM Program", _PROGRAM, {"program_name": _PROGRAM})
		self._ensure("CRM Program", _SIBLING_PROGRAM, {"program_name": _SIBLING_PROGRAM})
		self._ensure("CRM Lead Source", _SOURCE, {"source_name": _SOURCE})

		self.cfg = self._intake_form()
		self.dt = builder.doctype_name_for(self.cfg)
		builder.sync_form(self.cfg)
		self.web_form = builder.web_form_name_for(self.cfg)

	def tearDown(self):
		frappe.set_user("Administrator")
		for ld in frappe.get_all("CRM Lead", filters={"mobile_no": to_e164(_PHONE)}, pluck="name"):
			frappe.delete_doc("CRM Lead", ld, force=True, ignore_permissions=True)
		for wf in frappe.get_all("Web Form", filters={"doc_type": self.dt}, pluck="name"):
			frappe.delete_doc("Web Form", wf, force=True, ignore_permissions=True)
		if frappe.db.exists("DocType", self.dt):
			frappe.delete_doc("DocType", self.dt, force=True, ignore_permissions=True)
			frappe.db.delete("Singles", {"doctype": self.dt})
		for dt, name in reversed(self._made):
			if frappe.db.exists(dt, name):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		intake.bust_intake_doctype_cache()
		self._set_switch(_INTAKE_SWITCH, 0)
		self._set_switch(_DEDUP_SWITCH, 0)
		self._set_switch(_RATE_SWITCH, 0)

	# --- helpers ---------------------------------------------------------
	def _set_switch(self, key, on):
		if frappe.db.exists("CRM Tatva Automation", key):
			frappe.db.set_value("CRM Tatva Automation", key, "enabled", 1 if on else 0)

	def _ensure(self, doctype, name, values):
		if frappe.db.exists(doctype, name):
			return name
		d = frappe.new_doc(doctype)
		d.update(values)
		d.insert(ignore_permissions=True)
		self._made.append((doctype, d.name))
		return d.name

	def _intake_form(self, warn=1):
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
			"warn_if_already_enrolled": warn,
			"client_script": _OPERATOR_SCRIPT,
			"mappings": [
				{"source_field": _PHONE_QUESTION, "fieldtype": "Phone",
				 "target_table": "lead", "target_field": "mobile_no"},
				{"source_field": "patient_name", "target_table": "lead", "target_field": "first_name"},
			],
		})
		doc.insert(ignore_permissions=True)
		self._made.append(("CRM Intake Form", doc.name))
		frappe.clear_cache(doctype="CRM Intake Form")
		return frappe.get_cached_doc("CRM Intake Form", _FORM)

	def _reload_cfg(self):
		frappe.clear_cache(doctype="CRM Intake Form")
		self.cfg = frappe.get_cached_doc("CRM Intake Form", _FORM)
		return self.cfg

	def _set_flag(self, on):
		frappe.db.set_value("CRM Intake Form", _FORM, "warn_if_already_enrolled", 1 if on else 0)
		return self._reload_cfg()

	def _lead(self, group=_GROUP, program=_PROGRAM):
		doc = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Asha Existing", "mobile_no": _PHONE,
			"custom_vertical": _VERTICAL, "custom_group": group, "custom_current_program": program,
			"source": _SOURCE,
		})
		doc.insert(ignore_permissions=True)
		self._made.append(("CRM Lead", doc.name))
		return doc

	_THIS_FORM = object()  # sentinel: `or self.web_form` swallowed web_form="", the case under test

	def _ask(self, phone=_PHONE, web_form=_THIS_FORM):
		name = self.web_form if web_form is self._THIS_FORM else web_form
		return api.check_existing_patient(name, phone)

	def _patch_throttle(self, record):
		"""Count what the endpoint SPENDS, without a real request. `spend_rate_limit` needs redis and
		`request_ip`; the ordering under test is whether the call happens at all, and when."""
		from tatva_connect.intake import guards

		original = guards.throttle_existing_check
		guards.throttle_existing_check = lambda: record(1)
		self.addCleanup(setattr, guards, "throttle_existing_check", original)

	# --- it warns, on the line the anchor actually keys on -----------------
	def test_it_warns_when_the_number_is_already_a_lead_on_this_line(self):
		self._lead()
		answer = self._ask()
		self.assertTrue(answer["exists"])
		self.assertIn("already enrolled", answer["message"])

	def test_the_warning_describes_the_consequence_and_identifies_nothing(self):
		"""The reply leaves over an anonymous door, so a hit must say "known number" and not which
		programme, group or person it is known as. Asserted on the ACTUAL axis values this form is
		filed under, so widening the message to name any of them turns this red."""
		self._lead()
		message = self._ask()["message"]
		for leak in (_VERTICAL, _GROUP, _PROGRAM, _SIBLING_PROGRAM, "Asha Existing"):
			self.assertNotIn(leak, message, f"the warning must not name {leak}")

	def test_it_says_nothing_when_the_number_is_new(self):
		self.assertFalse(self._ask()["exists"])

	def test_a_typed_number_matches_the_stored_one_however_it_was_typed(self):
		"""The stored lead is +E.164 and the visitor types digits. Both go through the same
		canonicalisation the anchor is stored under, or the warning silently never fires."""
		self._lead()
		self.assertTrue(self._ask(phone="9000000031")["exists"])
		self.assertTrue(self._ask(phone="+91 90000 00031")["exists"])

	def test_the_same_number_on_another_group_is_not_this_form_s_patient(self):
		"""The anchor is per line: the same person on Zydus is a SEPARATE lead by design, and this
		form's submit would create one rather than merge — so warning here would be a lie."""
		self._lead(group=_OTHER_GROUP)
		self.assertFalse(self._ask()["exists"])

	def test_a_sibling_programme_of_the_same_group_still_warns(self):
		"""Program is NOT identity: a patient on a sibling programme of the same group is ONE lead, and
		submitting transitions them. The warning must fire — naming a programme would have been wrong
		here in particular, because the one this form would name is not the one they are on."""
		self._lead(program=_SIBLING_PROGRAM)
		self.assertTrue(self._ask()["exists"])

	# --- every not-a-yes is False: the form cannot start failing ------------
	def test_it_is_silent_while_the_flag_is_off(self):
		"""The default. An existing lead, and the form behaves exactly as it did before this feature."""
		self._lead()
		self._set_flag(0)
		self.assertFalse(self._ask()["exists"])

	def test_it_is_silent_while_the_feature_switch_is_off(self):
		"""Switch off means the fold never runs, so a submission merges onto nothing — there is no
		overwrite to warn about, and claiming one would be false."""
		self._lead()
		self._set_switch(_INTAKE_SWITCH, 0)
		self.assertFalse(self._ask()["exists"])

	def test_a_half_typed_number_answers_no_rather_than_throwing(self):
		"""It is asked on every change of the field, so it MUST tolerate what a half-typed number is."""
		self._lead()
		for junk in ("", "9", "90000", "not a number", None):
			self.assertFalse(self._ask(phone=junk)["exists"], junk)

	def test_a_web_form_that_is_not_an_intake_form_is_answered_nothing(self):
		"""The self-gate: the caller names a Web Form and the ONE brain says whether it is an enabled
		intake sink. Anything else is refused before a lead is ever read."""
		self._lead()
		self.assertFalse(self._ask(web_form="edit-profile")["exists"])
		self.assertFalse(self._ask(web_form="no-such-web-form")["exists"])
		self.assertFalse(self._ask(web_form="")["exists"])

	# --- bounded by intake's OWN limiter, spent before anything is inspected --
	def test_the_budget_is_spent_before_the_body_inspects_anything(self):
		"""ORDER is the whole assertion. Counting only once the number parsed left junk input free —
		no ceiling on the one caller a ceiling exists for. The spend must therefore happen for a
		request that is refused for EVERY other reason too: unknown form, dormant flag, fragment."""
		spent = []
		self._patch_throttle(spent.append)
		self._ask(web_form="edit-profile")          # not an intake form at all
		self._ask(phone="+91-98")                    # a fragment
		self._set_flag(0); self._ask(); self._set_flag(1)   # form not asking for the warning
		self.assertEqual(len(spent), 3, "every call must cost, whatever the answer turns out to be")

	def test_it_uses_intakes_one_limiter_and_not_a_second_one(self):
		"""Intake limits through `guards._bump` -> `utils.spend_rate_limit`, gated by
		`Intake::RateLimit::enforcement`, capped from `CRM Intake Settings`. This check must ride that
		and add no mechanism of its own — a module with two limiters has two places to change a cap,
		two switch behaviours, and no single answer to "what bounds intake"."""
		import inspect

		from tatva_connect.intake import guards

		self.assertNotIn("rate_limit", inspect.getsource(api), "a second limiter was introduced")
		src = inspect.getsource(guards.throttle_existing_check)
		self.assertIn("Intake::RateLimit::enforcement", src, "not on intake's switch")
		self.assertIn('_int_cfg("checks_per_hour")', src, "not on intake's settings cap")
		self.assertIn('_bump("check-ip"', src, "not on intake's counter, or sharing the submit key")

	def test_it_counts_nothing_while_intake_rate_limiting_is_dormant(self):
		"""Armed exactly like the submit throttle and the upload doorman — no special case."""
		self._lead()
		self._set_switch(_RATE_SWITCH, 0)
		key = "intake-rl:check-ip:unknown"
		frappe.cache.delete_value(key)
		self.assertTrue(self._ask()["exists"])
		self.assertIsNone(frappe.cache.get(frappe.cache.make_key(key)))

	def test_the_cap_comes_from_the_settings_single(self):
		"""Read through the same `_int_cfg` chain as its five neighbours, blank falling back to 120."""
		from tatva_connect.intake import guards

		self.assertEqual(guards._int_cfg("checks_per_hour"), guards.DEFAULTS["checks_per_hour"])
		frappe.db.set_value("CRM Intake Settings", None, "checks_per_hour", 250)
		frappe.clear_cache(doctype="CRM Intake Settings")
		self.addCleanup(frappe.clear_cache, doctype="CRM Intake Settings")
		self.addCleanup(frappe.db.set_value, "CRM Intake Settings", None, "checks_per_hour", 0)
		self.assertEqual(guards._int_cfg("checks_per_hour"), 250)

	# --- the published form carries the confirm, bound to the RIGHT field ---
	def test_the_published_script_binds_the_contract_s_own_phone_question(self):
		script = frappe.db.get_value("Web Form", self.web_form, "client_script")
		self.assertIn(f'"{_PHONE_QUESTION}"', script)
		self.assertNotIn('get_field("phone")', script, "bound by convention, not by the contract")
		self.assertIn("check_existing_patient", script)
		self.assertIn(_OPERATOR_SCRIPT, script, "the operator's own script survives beside it")

	def test_with_the_flag_off_the_published_script_is_the_operator_s_and_nothing_else(self):
		"""The non-breaking guarantee, at the byte level: an unarmed form's published script is
		EXACTLY what the operator wrote — this feature adds no code to a form that did not ask."""
		self._set_flag(0)
		builder.sync_form(self._reload_cfg())
		self.assertEqual(
			frappe.db.get_value("Web Form", self.web_form, "client_script"), _OPERATOR_SCRIPT
		)

	# --- and the submit still does exactly what it always did ---------------
	def test_the_submit_path_still_merges_onto_the_existing_lead(self):
		"""The feature tells; it does not change what telling is about. A submission for a number that
		already has a lead still folds onto that ONE lead, as it did before any of this existed."""
		before = self._lead().name
		frappe.get_doc({
			"doctype": self.dt, "intake_form": self.cfg.name,
			_PHONE_QUESTION: _PHONE, "patient_name": "Asha Resubmitted",
		}).insert(ignore_permissions=True)

		leads = frappe.get_all("CRM Lead", filters={"mobile_no": to_e164(_PHONE)}, pluck="name")
		self.assertEqual(leads, [before], "one patient, one lead — merged, never duplicated")
		self.assertEqual(frappe.db.get_value("CRM Lead", before, "first_name"), "Asha Resubmitted")
