# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Runs every registry case as a subTest: impersonate the principal, hit the real surface, and
judge the verdict against the native oracle, never a hardcoded expectation."""

import re

import frappe

from tatva_connect.lead import detail as lead_detail_mod
from tatva_connect.tests.authz import generator, grains, roster
from tatva_connect.tests.authz.base import AuthzTestCase, dispatch, set_user
from tatva_connect.tests.authz.oracle import (
	native_doctype_capability,
	native_permitted_fields,
	native_visible_names,
	native_would_allow,
)
from tatva_connect.tests.authz.registry import cases as registry_cases

# The grain fields — A7 asserts a grain user can READ these but not EDIT a lead out of its grain.
GRAIN_FIELDNAMES = ("custom_vertical", "custom_group", "custom_current_program")
# The runtime switch that turns on grain enforcement (off = documented stock exposure).
_GRAIN_SWITCH = "Lead::CRM Lead::grain"

# A9 PQC shape per child doctype: (has_assigned_to, parent reference column).
_A9_SHAPE = {
	"CRM Task": (True, "reference_docname"),
	"CRM Call Log": (False, "reference_docname"),
	"FCRM Note": (False, "reference_docname"),
	"WhatsApp Message": (False, "reference_name"),
}

# A10 kwargs per guarded method, keyed on the native path so dispatch goes through the override wrapper.
_A10_KW = {
	"crm.integrations.api.get_recording_url": lambda t: {"call_log_name": t},
	"crm.integrations.api.add_task_to_call_log": lambda t: {
		"call_sid": t,
		"task": {"title": "authz", "status": "Todo"},
	},
	"crm.integrations.api.add_note_to_call_log": lambda t: {"call_sid": t, "note": {"content": "authz"}},
	"crm.api.doc.get_assigned_users": lambda t: {"doctype": "CRM Lead", "name": t},
	"crm.api.doc.get_linked_docs_of_document": lambda t: {"doctype": "CRM Lead", "docname": t},
	"crm.api.whatsapp.get_whatsapp_messages": lambda t: {
		"reference_doctype": "CRM Lead",
		"reference_name": t,
	},
}


class TestRegistryCases(AuthzTestCase):
	# A14 submission sink, created once so its DDL commit cannot break each case's savepoint.
	A14_SINK = "Intake Authz A14 Sink"

	@classmethod
	def _ensure_a14_sink(cls):
		if not frappe.db.exists("DocType", cls.A14_SINK):
			frappe.get_doc(
				{
					"doctype": "DocType",
					"name": cls.A14_SINK,
					"module": "Intake",
					"custom": 1,
					"autoname": "hash",
					"fields": [
						{"fieldname": f, "fieldtype": ft, "label": f}
						for f, ft in [
							("intake_form", "Data"),
							("phone", "Data"),
							("patient_name", "Data"),
							("state_manual", "Data"),
							("doctor", "Data"),
							("doctor_manual", "Data"),
							("remarks", "Small Text"),
						]
					],
					"permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1}],
				}
			).insert(ignore_permissions=True)
			frappe.db.commit()  # DDL already committed implicitly; make the DocType row durable too

	@classmethod
	def setUpClass(cls):
		super().setUpClass()  # comms-off interlock + class commit floor (rolled back per class)
		cls._ensure_a14_sink()  # DDL BEFORE the (uncommitted) seed so its implicit commit can't leak it
		# Seeds roster, grain rules, partner mapping and leads uncommitted, so the class rollback clears them.
		cls.seeded = generator.seed(commit=False)

	# ---- target resolution: a concrete seeded lead for a case's `target` semantics ----------------

	def _principal_grain(self, principal):
		"""The (vertical, group, program) the persona's grain_key resolves to (None for non-grain)."""
		key = roster.by_persona(principal)["grain_key"]
		if not key:
			return None
		return next(g for g in grains.GRAINS if g["key"] == key)

	def _lead_in_grain(self, g):
		"""A seeded lead in grain `g`; a fixture lookup, not an authz check."""
		return frappe.db.get_value(
			"CRM Lead",
			{
				"custom_vertical": g["vertical"],
				"custom_group": g["group"],
				"custom_current_program": g["program"],
				"first_name": ["like", f"%{generator.TAG}%"],
			},
			"name",
		)

	def _resolve_target(self, c):
		"""Map a case's target token (in_grain, out_of_grain, same_program_diff_vertical) to a seeded
		lead name, or None when it does not apply."""
		own = self._principal_grain(c.principal)
		if c.target == "in_grain":
			return self._lead_in_grain(own) if own else None
		if c.target == "out_of_grain":
			other = next(
				(
					g
					for g in grains.GRAINS
					if not own or (g["vertical"], g["group"]) != (own["vertical"], own["group"])
				),
				None,
			)
			return self._lead_in_grain(other) if other else None
		if c.target == "same_program_diff_vertical":
			# grain_5 shares grain_4's program name but differs on the other axes.
			return self._lead_in_grain(self._principal_grain("grain_5"))
		return None

	@staticmethod
	def _principal_user(c):
		"""The login to impersonate: a roster email, or 'Guest' for the guest persona."""
		if c.principal == "Guest":
			return "Guest"
		return roster.email(c.principal)

	# ---- A1: horizontal grain leak (list; oracle = visible_names) --------------------------------

	def test_A1_horizontal_grain_leak(self):
		for c in registry_cases.cases_for("A1"):
			with self.subTest(case=c.id):
				self._run_list_case(c)

	# ---- A2: same-program / different-vertical trap (list; oracle = visible_names) ---------------

	def test_A2_same_program_diff_vertical(self):
		for c in registry_cases.cases_for("A2"):
			with self.subTest(case=c.id):
				# grain_4 must see no grain_5 lead despite the shared program name.
				self._run_list_case(c)

	def _run_list_case(self, c):
		if c.surface != "list":
			self.fail(f"A1/A2 runner only handles surface 'list'; got {c.surface}")
		user = self._principal_user(c)
		target = self._resolve_target(c)
		if target is None:
			self.fail(f"no seeded {c.target} target for case {c.id}")
		visible = native_visible_names(user, c.doctype)  # the native ceiling (runs PQC)
		if c.expected == "deny":
			self.assertNotIn(
				target,
				visible,
				f"A-LEAK: {user} ({c.principal}) can see out-of-grain lead {target} — native list must not expose it",
			)
		else:
			self.assertIn(
				target,
				visible,
				f"REGRESSION: {user} ({c.principal}) cannot see its own in-grain lead {target}",
			)

	# ---- A4: grain-vs-role restriction wins (field; MUTATES → savepoint) -------------------------

	def test_A4_grain_vs_role_restriction(self):
		for c in registry_cases.cases_for("A4"):
			with self.subTest(case=c.id):
				self._run_a4_case(c)

	def _run_a4_case(self, c):
		if c.surface != "field":
			self.fail(f"A4 runner only handles surface 'field'; got {c.surface}")
		user = self._principal_user(c)
		target = self._resolve_target(c)
		if target is None:
			self.fail(f"no seeded {c.target} target for case {c.id}")
		role = roster.by_persona(c.principal)["roles"][0]  # the persona's primary desk role
		# Pick a real lead-surface catalog field this role would otherwise see, then restrict it.
		victim_key = self._a4_restrictable_key(user)
		if victim_key is None:
			self.fail(f"no entitled lead-detail catalog field to restrict for {user}")
		save_point = "authz_a4_{}".format(c.id.replace("-", "_"))
		frappe.db.savepoint(save_point)
		try:
			frappe.get_doc(
				{
					"doctype": "CRM Lead Field Restriction",
					"role": role,
					"field": victim_key,
				}
			).insert(ignore_permissions=True)
			# Restrictions are request-cached; clear so the resolver re-reads.
			from tatva_connect.access import entitlement

			setattr(frappe.local, entitlement._RESTRICT_CACHE, {})
			with set_user(user):
				payload = lead_detail_mod.lead_detail(target)
			rendered_keys = {
				f.get("field_key") for s in payload.get("sections", []) for f in s.get("fields", [])
			}
			self.assertNotIn(
				victim_key,
				rendered_keys,
				f"A4: field {victim_key} restricted for role {role} still renders in lead_detail for {user}",
			)
		finally:
			frappe.db.rollback(save_point=save_point)
			from tatva_connect.access import entitlement

			setattr(frappe.local, entitlement._RESTRICT_CACHE, {})

	@staticmethod
	def _a4_restrictable_key(user):
		"""A non-universal lead-detail field the user can see now, to restrict and prove gone.
		None if nothing qualifies."""
		from tatva_connect.access import entitlement

		with set_user(user):
			catalog = lead_detail_mod._catalog_rows()
			visible = entitlement.resolve_fields(catalog, entitlement.entitled_grains(), frappe.get_roles())
		for key in visible:
			if not entitlement.is_universal_field(key):
				return key
		return None

	# ---- A7: a grain user can read a lead's grain fields but not move it out of its grain -------

	def test_A7_grain_field_read_allowed_edit_denied(self):
		for c in registry_cases.cases_for("A7"):
			with self.subTest(case=c.id):
				if c.surface == "smartview":
					self._run_smartview_case(c)
				elif c.action == "field_read" and c.expected == "allow":
					self._run_a7_read_allowed(c)
				elif c.action == "write" and c.expected == "deny":
					self._run_a7_edit_denied(c)
				else:
					self.fail(f"A7 runner: unhandled case shape {c.id}")

	def _run_a7_read_allowed(self, c):
		"""A grain user can read its own grain fields; that is by design, since the guard is on edit."""
		user = self._principal_user(c)
		permitted = native_permitted_fields(user, c.doctype)
		missing = [fn for fn in GRAIN_FIELDNAMES if fn not in permitted]
		self.assertEqual(
			missing,
			[],
			f"A7: grain user {user} is missing READ access to its own grain field(s) {missing} — read is the "
			"intended model (Sales User sees their grain)",
		)

	def _run_a7_edit_denied(self, c):
		"""With the grain switch on, a grain user cannot move a lead to a program outside its entitlement.
		Switch off is the documented stock exposure, so the test turns it on first."""
		user = self._principal_user(c)
		own = self._principal_grain(c.principal)
		lead = self._lead_in_grain(own) if own else None
		if lead is None:
			self.fail(f"no seeded in-grain lead for case {c.id}")
		out_program = next(g["program"] for g in grains.GRAINS if g["program"] != own["program"])
		save_point = "authz_a7_{}".format(c.id.replace("-", "_"))
		frappe.db.savepoint(save_point)
		try:
			frappe.db.set_value("CRM Tatva Automation", _GRAIN_SWITCH, "enabled", 1)
			frappe.clear_cache()
			with set_user(user):
				doc = frappe.get_doc(c.doctype, lead)
				doc.custom_current_program = out_program
				with self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
					doc.save()
		finally:
			frappe.db.rollback(save_point=save_point)
			frappe.clear_cache()

	# ---- A6: bypass-write escalation (oracle = would_allow) --------------------------------------

	def test_A6_bypass_write_escalation(self):
		for c in registry_cases.cases_for("A6"):
			with self.subTest(case=c.id):
				self._run_a6_case(c)

	def _run_a6_case(self, c):
		user = self._principal_user(c)
		target = self._resolve_target(c)
		if target is None:
			self.fail(f"no seeded {c.target} target for case {c.id}")
		doc = frappe.get_doc(c.doctype, target)
		allowed = native_would_allow(user, c.doctype, c.action, doc)  # doc mandatory
		if c.expected == "deny":
			self.assertFalse(
				allowed,
				f"A6 ESCALATION: {user} ({c.principal}) may {c.action} out-of-grain {c.doctype}/{target} natively",
			)
		else:
			self.assertTrue(
				allowed,
				f"A6 REGRESSION: {user} ({c.principal}) cannot {c.action} in-scope {c.doctype}/{target}",
			)

	# ---- A3: privilege escalation (deny-sweep; oracle = capability) ------------------------------

	def test_A3_privilege_escalation(self):
		for c in registry_cases.cases_for("A3"):
			with self.subTest(case=c.id):
				self._run_capability_deny_case(c)

	# ---- A11: cross-app doctype leak (deny-sweep; oracle = capability) ---------------------------

	def test_A11_cross_app_leak(self):
		for c in registry_cases.cases_for("A11"):
			with self.subTest(case=c.id):
				self._run_capability_deny_case(c)

	def _run_capability_deny_case(self, c):
		"""Doctype-level deny sweep for A3 and A11; the only safe doc=None oracle use, since deny
		cannot be a false negative."""
		if c.expected != "deny":
			self.fail(f"capability deny-sweep runner only judges DENY cases; {c.id} expects allow")
		user = self._principal_user(c)
		allowed = native_doctype_capability(user, c.doctype, c.action)
		self.assertFalse(
			allowed,
			f"{c.attack} BREACH: {user} ({c.principal}) has {c.action} capability on {c.doctype}",
		)

	# ---- A13: partner-mapping abuse (inner-core gate; drive AS the partner) -----------------------
	# The @_api endpoints swallow exceptions into an envelope, so these cases drive the inner cores.

	def test_A13_partner_mapping_abuse(self):
		for c in registry_cases.cases_for("A13"):
			with self.subTest(case=c.id):
				self._run_a13_partner_case(c)

	@staticmethod
	def _plant_call_log(lead_name, external_id):
		"""Plant a CRM Call Log on `lead_name` with `external_id`, since the generator seeds none.
		The caller holds the savepoint."""
		doc = frappe.new_doc("CRM Call Log")
		doc.id = f"AUTHZ-A13-{frappe.generate_hash(length=8)}"  # autoname is field:id
		doc.set("custom_external_id", external_id)
		doc.type = "Incoming"
		doc.status = "Completed"
		setattr(doc, "from", "+910000000000")  # from/to are required; an empty string counts as missing
		doc.to = "+910000000001"
		doc.reference_doctype = "CRM Lead"
		doc.reference_docname = lead_name
		doc.insert(ignore_permissions=True)
		return doc.name

	def _run_a13_partner_case(self, c):
		from tatva_connect.api import partner_activity, partner_call
		from tatva_connect.api._base import _resolve_caller

		user = roster.email("partner")  # bound to grain_1 via the CRM Lead API Mapping seed()
		# resolve_lead raises a generic not-found; the wide catch keeps any refusal from passing falsely.
		refusals = (frappe.DoesNotExistError, frappe.PermissionError, frappe.ValidationError)

		# A disabled mapping denies the partner all access.
		if c.id == "A13-partner-disabled-mapping":
			save_point = "authz_a13_disabled"
			frappe.db.savepoint(save_point)
			try:
				frappe.db.set_value("CRM Lead API Mapping", {"partner_user": user}, "enabled", 0)
				with set_user(user):
					with self.assertRaises(frappe.PermissionError):
						_resolve_caller()
			finally:
				frappe.db.rollback(save_point=save_point)
			return

		out_lead = self._lead_in_grain(grains.GRAINS[2])  # grain_3 — outside the partner's grain_1
		if out_lead is None:
			self.fail(f"no seeded grain_3 (out-of-grain) lead for case {c.id}")

		# external_id is a label, not an address: a colliding id must not touch the grain_3 row.
		if c.id == "A13-partner-extid-collision-no-cross-tenant":
			save_point = "authz_a13_collision"
			external_id = "AUTHZ-A13-COLLIDE"
			in_lead = self._lead_in_grain(grains.GRAINS[0])  # grain_1 — the partner's OWN lead
			if in_lead is None:
				self.fail(f"no seeded grain_1 (in-grain) lead for case {c.id}")
			frappe.db.savepoint(save_point)
			original_form_dict = frappe.form_dict
			try:
				with set_user(user):
					_u, mp, is_sysmgr = _resolve_caller()
				planted = self._plant_call_log(out_lead, external_id)  # as Administrator, on grain_3
				before = frappe.db.get_value(
					"CRM Call Log", planted, ["reference_docname", "status", "duration"], as_dict=True
				)

				# The partner sends the SAME external_id against their OWN lead.
				frappe.form_dict = frappe._dict(
					{
						"lead": in_lead,
						"external_id": external_id,
						"direction": "Inbound",
						"from_number": "+919000000201",
						"to_number": "+919000000202",
						"duration": 99,
					}
				)
				with set_user(user):
					view, action = partner_call._create_one(frappe.form_dict, mp, is_sysmgr)

				self.assertEqual(action, "created", "A13: a colliding external_id must still CREATE")
				self.assertNotEqual(
					view["name"],
					planted,
					"A13 CROSS-TENANT: a colliding external_id addressed the grain_3 Call Log — "
					"external_id must never resolve a record",
				)
				self.assertEqual(
					view["lead"], in_lead, "A13: the new call must land on the partner's own lead"
				)
				after = frappe.db.get_value(
					"CRM Call Log", planted, ["reference_docname", "status", "duration"], as_dict=True
				)
				self.assertEqual(
					dict(before),
					dict(after),
					f"A13 CROSS-TENANT: the grain_3 Call Log was mutated by a colliding external_id "
					f"{external_id} — a label must never touch another tenant's row",
				)
			finally:
				frappe.form_dict = original_form_dict
				frappe.db.rollback(save_point=save_point)
			return

		# Out-of-grain creates are refused before any write, so only form_dict needs restoring.
		with set_user(user):
			_u, mp, is_sysmgr = _resolve_caller()
		original_form_dict = frappe.form_dict
		try:
			if c.id == "A13-partner-callog-out-of-grain-create":
				frappe.form_dict = frappe._dict(
					{
						"lead": out_lead,
						"external_id": "AUTHZ-A13-CL",
						"direction": "Inbound",
						"from_number": "+919000000201",
						"to_number": "+919000000202",
					}
				)
				core = lambda: partner_call._create_one(frappe.form_dict, mp, is_sysmgr)  # noqa: E731
			elif c.id == "A13-partner-activity-out-of-grain-create":
				# A real type on the foreign lead's own grain, so only the grain gate can refuse it.
				g = grains.GRAINS[2]
				task_type = frappe.get_doc({"doctype": "CRM Task Type", "type_name": "ZZ Authz Probe", "vertical": g["vertical"],
				                            "group": g["group"], "program": g["program"]}).insert(ignore_permissions=True).name
				frappe.form_dict = frappe._dict({"lead": out_lead, "external_id": "AUTHZ-A13-AC", "task_type": task_type})
				core = lambda: partner_activity._create_one(frappe.form_dict, mp, is_sysmgr)  # noqa: E731
			else:
				self.fail(f"A13 runner: unhandled case {c.id}")
				return
			with set_user(user):
				with self.assertRaises(
					refusals,
					msg=f"A13 ESCALATION: partner reached a grain_3 lead via {c.id} before the grain gate",
				):
					core()
		finally:
			frappe.form_dict = original_form_dict

	# ---- A14: public-intake guest abuse (drive the intake fold AS Guest) --------------------------
	# A Guest submit cannot reroute across grain, grow a master, or write outside its own lead.

	def test_A14_guest_intake(self):
		for c in registry_cases.cases_for("A14"):
			with self.subTest(case=c.id):
				self._run_a14_guest_intake(c)

	def _a14_intake_form(self, g, mappings):
		"""A forced-routing CRM Intake Form on grain `g`, kept in memory so no DDL breaks the savepoint;
		the fold reads it off the object."""
		doc = frappe.get_doc(
			{
				"doctype": "CRM Intake Form",
				"form_name": f"authz-test-a14-{frappe.generate_hash(length=6)}",
				"enabled": 1,
				"custom_vertical": g["vertical"],
				"custom_group": g["group"],
				"custom_current_program": g["program"],
				"mappings": mappings,
			}
		)
		doc.name = doc.form_name  # the fold stamps cfg.name as provenance
		return doc

	def _a14_submission(self, cfg, values):
		"""A submission row on the A14 sink carrying only the fields the mappings read."""
		doc = frappe.get_doc({"doctype": self.A14_SINK, "intake_form": cfg.name, **values})
		doc.insert(ignore_permissions=True, ignore_mandatory=True)
		return doc

	def _a14_lead_for(self, phone):
		"""Find the lead the fold wrote, by phone, since the sink has no `lead` field."""
		digits = re.sub(r"\D", "", phone)[-10:]
		name = frappe.db.get_value("CRM Lead", {"mobile_no": ["like", f"%{digits}%"]}, "name")
		return frappe.get_doc("CRM Lead", name) if name else None

	def _run_a14_guest_intake(self, c):
		from tatva_connect.intake import intake as intake_mod
		from tatva_connect.taxonomy.normalize import normalize_display

		g = grains.GRAINS[0]  # the FORM's grain (grain_1)
		foreign = grains.GRAINS[2]  # grain_3 — the smuggled foreign grain
		save_point = "authz_a14_{}".format(c.id.replace("-", "_"))
		frappe.db.savepoint(save_point)
		try:
			if c.id == "A14-guest-routing-forced":
				# Foreign grain values mapped onto routing axes must be dropped for the form's own grain.
				cfg = self._a14_intake_form(
					g,
					[
						{
							"source_field": "phone",
							"fieldtype": "Phone",
							"target_table": "lead",
							"target_field": "mobile_no",
						},
						{
							"source_field": "patient_name",
							"target_table": "lead",
							"target_field": "custom_vertical",
						},
						{
							"source_field": "state_manual",
							"target_table": "lead",
							"target_field": "custom_group",
						},
						{
							"source_field": "doctor_manual",
							"target_table": "lead",
							"target_field": "custom_current_program",
						},
					],
				)
				sub = self._a14_submission(
					cfg,
					{
						"phone": "+91 9990077001",
						"patient_name": foreign["vertical"],
						"state_manual": foreign["group"],
						"doctor_manual": foreign["program"],
					},
				)
				with set_user("Guest"):
					intake_mod._fold_submission_to_lead(sub, cfg)
				lead = self._a14_lead_for("+91 9990077001")
				self.assertEqual(
					lead.custom_vertical,
					g["vertical"],
					f"A14: Guest smuggled vertical {foreign['vertical']} stuck on the lead",
				)
				self.assertEqual(
					lead.custom_group,
					g["group"],
					f"A14: Guest smuggled group {foreign['group']} stuck on the lead",
				)
				self.assertEqual(
					lead.custom_current_program,
					g["program"],
					f"A14: Guest smuggled program {foreign['program']} stuck on the lead",
				)
				self.assertNotEqual(
					lead.custom_vertical,
					foreign["vertical"],
					"A14 ROUTING ESCALATION: foreign vertical was accepted from a Guest payload",
				)

			elif c.id == "A14-guest-no-master-growth":
				# This master is growable, so only the Guest guard in intake stops a Guest from growing it.
				master = "CRM Side Effect Option"
				cfg = self._a14_intake_form(
					g,
					[
						{
							"source_field": "phone",
							"fieldtype": "Phone",
							"target_table": "lead",
							"target_field": "mobile_no",
						},
						{
							"source_field": "doctor",
							"manual_field": "doctor_manual",
							"master_doctype": master,
							"target_table": "note",
							"target_field": "option_name",
						},
					],
				)
				typed = "Authz Guest Side Effect"
				canonical = normalize_display(typed)
				sub = self._a14_submission(cfg, {"phone": "+91 9990077002", "doctor_manual": typed})
				before = frappe.db.count(master)
				with set_user("Guest"):
					intake_mod._fold_submission_to_lead(sub, cfg)
				# (1) Guest did not grow the master, and the typed value is kept as text.
				self.assertEqual(
					frappe.db.count(master),
					before,
					f"A14 MASTER GROWTH: a Guest submit grew {master} — the Guest guard (intake.py:355) is gone",
				)
				lead = self._a14_lead_for("+91 9990077002")
				note = frappe.db.get_value(
					"FCRM Note",
					{"reference_doctype": "CRM Lead", "reference_docname": lead.name, "title": "option_name"},
					"content",
				)
				self.assertEqual(
					note,
					canonical,
					f"A14: the typed value was not recorded as canonical text on the lead (note={note})",
				)
				# (2) The same call as an authed user grows it by one, so the block above is Guest-specific.
				authed_before = frappe.db.count(master)
				grown = intake_mod._ensure_master(master, "option_name", "Authz Authed Side Effect")
				self.assertEqual(
					frappe.db.count(master),
					authed_before + 1,
					f"A14: an authed _ensure_master did NOT grow {master} — the master must be growable "
					"for the Guest differential to mean anything",
				)
				self.assertEqual(grown, normalize_display("Authz Authed Side Effect"))

			elif c.id == "A14-guest-note-scope":
				# The fold's FCRM Note must reference only its own lead.
				cfg = self._a14_intake_form(
					g,
					[
						{
							"source_field": "phone",
							"fieldtype": "Phone",
							"target_table": "lead",
							"target_field": "mobile_no",
						},
						{
							"source_field": "remarks",
							"target_table": "note",
							"target_field": "Enrolment Remarks",
						},
					],
				)
				sub = self._a14_submission(cfg, {"phone": "+91 9990077003", "remarks": "guest note body"})
				with set_user("Guest"):
					intake_mod._fold_submission_to_lead(sub, cfg)
				lead = self._a14_lead_for("+91 9990077003")
				refs = frappe.get_all(
					"FCRM Note",
					filters={"title": "Enrolment Remarks", "content": "guest note body"},
					pluck="reference_docname",
				)
				self.assertTrue(refs, "A14: the Guest fold did not write its FCRM Note")
				self.assertTrue(
					all(r == lead.name for r in refs),
					f"A14 NOTE SCOPE: a Guest note referenced a lead other than the fold's own {lead.name}: {refs}",
				)
			else:
				self.fail(f"A14 runner: unhandled case {c.id}")
		finally:
			frappe.db.rollback(save_point=save_point)
			frappe.clear_cache()

	# ---- A7 / A12: Smart View authoring clamp (drive upsert_view AS the grain user) ---------------
	# A Smart View with an out-of-grain axis or column is refused before save, so nothing is written.

	def test_A12_userperm_docshare_overgrant(self):
		for c in registry_cases.cases_for("A12"):
			with self.subTest(case=c.id):
				if c.surface == "smartview":
					self._run_smartview_case(c)
				else:
					self._run_list_case(c)

	def _run_smartview_case(self, c):
		from tatva_connect.smartview import api as smartview_api

		user = self._principal_user(c)
		own = self._principal_grain(c.principal)
		if own is None:
			self.fail(f"smartview case {c.id} needs a grain principal")
		out = next(
			(g for g in grains.GRAINS if (g["vertical"], g["group"]) != (own["vertical"], own["group"])), None
		)
		if out is None:
			self.fail(f"no out-of-grain grain for case {c.id}")

		if c.attack == "A12":
			# An out-of-grain axis is refused.
			view = {
				"label": "authz-clamp",
				"base_object": "Lead",
				"vertical": out["vertical"],
				"group": out["group"],
				"program": out["program"],
			}
		else:
			# A real catalog column ticked only by the foreign grain's contract: only the grain clamp can refuse it.
			from tatva_connect.tests.api import partner_fixture

			column = partner_fixture.stock_catalog_rows(1)[0]
			contract = frappe.db.get_value("CRM Lead API Mapping", {"is_internal": 1, "vertical": out["vertical"],
			                                "crm_group": out["group"], "program": out["program"]}) or frappe.get_doc({
				"doctype": "CRM Lead API Mapping", "contract_name": f"authz {out['key']} internal", "enabled": 1, "is_internal": 1,
				"vertical": out["vertical"], "crm_group": out["group"], "program": out["program"]}).insert(ignore_permissions=True).name
			doc = frappe.get_doc("CRM Lead API Mapping", contract)
			doc.append("allowed_fields", {"field": column})
			doc.save(ignore_permissions=True)
			for bucket in ("tatva_connect:internal_contract_ticks", "tatva_connect:internal_universal_fields"):
				if hasattr(frappe.local, bucket):
					delattr(frappe.local, bucket)
			view = {
				"label": "authz-col",
				"base_object": "Lead",
				"vertical": own["vertical"],
				"group": own["group"],
				"program": own["program"],
				"columns": [column],
			}
		# Either error is a refusal; catching both keeps neither path from passing falsely.
		with set_user(user):
			with self.assertRaises(
				(frappe.PermissionError, frappe.ValidationError),
				msg=f"A SMART-VIEW LEAK: {user} ({c.principal}) persisted a cross-tenant view via {c.id}",
			):
				smartview_api.upsert_view(view)

	# ---- A9: child-doctype visibility inheritance (the row-visibility brain, access/visibility.py) ---
	# With the switch on, a child on a grain_1 lead inherits grain_2's denial; off is stock CRM.

	def test_A9_child_scope_inheritance(self):
		for c in registry_cases.cases_for("A9"):
			with self.subTest(case=c.id):
				self._run_a9_case(c)

	def _a9_make_child(self, doctype, lead, assigned_to=None, links_only=False):
		"""Plant a child of `doctype` on `lead`. WhatsApp Message skips before_insert via db_insert,
		since its provider pipeline needs a tenant."""
		if doctype == "CRM Task":
			doc = frappe.new_doc("CRM Task")
			doc.title, doc.status = "A9 Scope Task", "Todo"
			doc.reference_doctype, doc.reference_docname = "CRM Lead", lead
			if assigned_to:
				doc.assigned_to = assigned_to
			doc.insert(ignore_permissions=True)
			return doc
		if doctype == "CRM Call Log":
			doc = frappe.new_doc("CRM Call Log")
			doc.id = f"A9-{frappe.generate_hash(length=8)}"
			doc.type, doc.status = "Incoming", "Completed"
			setattr(doc, "from", "+910000000000")
			doc.to = "+910000000001"
			if links_only:
				doc.append("links", {"link_doctype": "CRM Lead", "link_name": lead})
			else:
				doc.reference_doctype, doc.reference_docname = "CRM Lead", lead
			doc.insert(ignore_permissions=True)
			return doc
		if doctype == "FCRM Note":
			doc = frappe.new_doc("FCRM Note")
			doc.title, doc.content = "A9 Scope Note", "secret"
			doc.reference_doctype, doc.reference_docname = "CRM Lead", lead
			doc.insert(ignore_permissions=True)
			return doc
		if doctype == "WhatsApp Message":
			doc = frappe.new_doc("WhatsApp Message")
			doc.type, doc.content_type, doc.message = "Incoming", "text", "secret"
			doc.message_id = frappe.generate_hash(length=12)
			doc.reference_doctype, doc.reference_name = "CRM Lead", lead
			doc.name = doc.message_id
			doc.db_insert()
			return frappe.get_doc("WhatsApp Message", doc.name)
		raise ValueError(f"A9: no child factory for {doctype}")

	def _run_a9_case(self, c):
		from tatva_connect.access import visibility

		scope = visibility.SCOPED.get(c.doctype)
		if not scope or not scope.switch:
			self.fail(f"A9: {c.doctype} is not a switchable row-scoped doctype")
		switch = scope.switch
		in_user = roster.email("grain_1")  # owner of the grain_1 lead the child hangs off
		out_user = roster.email("grain_2")  # the peer that natively cannot read that lead
		lead = self._lead_in_grain(grains.GRAINS[0])
		if lead is None:
			self.fail(f"no seeded grain_1 lead for case {c.id}")
		save_point = "authz_a9_{}".format(c.id.replace("-", "_"))
		frappe.db.savepoint(save_point)
		try:
			if c.surface == "child_off":
				# Switch off is stock CRM: no PQC, and the peer can read the child.
				frappe.db.set_value("CRM Tatva Automation", switch, "enabled", 0)
				frappe.clear_cache()
				child = self._a9_make_child(c.doctype, lead)
				self.assertEqual(
					visibility.scoped_pqc(c.doctype, out_user),
					"",
					f"A9: {c.doctype} switch OFF must yield NO scoped PQC (stock CRM)",
				)
				self.assertTrue(
					visibility.scoped_has_permission(child, "read", out_user),
					f"A9: {c.doctype} switch OFF must expose the child to any role-permitted user "
					"(the documented stock-CRM exposure delta)",
				)
				return
			# every other branch is the switch-ON (scoped) state.
			frappe.db.set_value("CRM Tatva Automation", switch, "enabled", 1)
			frappe.clear_cache()
			if c.surface == "child_shape":
				pqc = visibility.scoped_pqc(c.doctype, out_user)
				has_assigned, ref_col = _A9_SHAPE[c.doctype]
				self.assertIn(
					f"`tab{c.doctype}`.`owner`=",
					pqc,
					f"A9 shape: {c.doctype} PQC missing the owner self-ownership clause",
				)
				self.assertIn(
					"reference_doctype",
					pqc,
					f"A9 shape: {c.doctype} PQC missing the reference_doctype parent clause",
				)
				self.assertIn(ref_col, pqc, f"A9 shape: {c.doctype} PQC missing the {ref_col} column")
				if has_assigned:
					self.assertIn(
						"assigned_to", pqc, f"A9 shape: {c.doctype} PQC missing its assigned_to clause"
					)
				else:
					self.assertNotIn(
						"assigned_to",
						pqc,
						f"A9 shape: {c.doctype} PQC has an assigned_to clause it should not",
					)
				if ref_col == "reference_name":
					self.assertNotIn(
						"reference_docname",
						pqc,
						"A9 shape: WhatsApp Message must key on reference_name, not reference_docname",
					)
				return
			if c.surface == "child_orphan":
				# A links-only Call Log has no parent, so it fails closed even for the in-scope user.
				child = self._a9_make_child(c.doctype, lead, links_only=True)
				self.assertFalse(
					visibility.scoped_has_permission(child, "read", in_user),
					"A9: a links-only Call Log (no reference parent) must fail closed even for the "
					"in-scope user",
				)
				return
			if c.surface == "child_assignee":
				# The assignee sees their own task on a parent they cannot read.
				child = self._a9_make_child(c.doctype, lead, assigned_to=out_user)
				self.assertTrue(
					visibility.scoped_has_permission(child, "read", out_user),
					"A9: an assignee must see their own task even on a parent they cannot read",
				)
				return
			# child_scope: switch on blocks the peer and keeps the in-scope owner.
			child = self._a9_make_child(c.doctype, lead)
			self.assertTrue(
				visibility.scoped_has_permission(child, "read", in_user),
				f"A9 REGRESSION: in-scope grain_1 denied its own {c.doctype} {child.name}",
			)
			self.assertFalse(
				visibility.scoped_has_permission(child, "read", out_user),
				f"A9 LEAK: out-of-scope grain_2 saw {c.doctype} {child.name} on a lead it cannot read",
			)
			with set_user(out_user):
				self.assertFalse(
					frappe.has_permission(c.doctype, "read", child.name),
					f"A9 LEAK: frappe.has_permission exposed {c.doctype} {child.name} to grain_2 "
					"(the wired has_permission hook must deny too)",
				)
		finally:
			frappe.db.rollback(save_point=save_point)
			frappe.clear_cache()

	# ---- A10: native-method bypass (the 11 access.native_guards wrappers) --------------------------
	# Runs through the real override dispatch, since a direct import would skip the guard.

	def test_A10_native_method_bypass(self):
		for c in registry_cases.cases_for("A10"):
			with self.subTest(case=c.id):
				self._run_a10_case(c)

	def _run_a10_case(self, c):
		user = self._principal_user(c)
		lead = self._resolve_target(c)
		if lead is None:
			self.fail(f"no seeded {c.target} lead for case {c.id}")
		save_point = "authz_a10_{}".format(c.id.replace("-", "_"))
		frappe.db.savepoint(save_point)
		try:
			if c.doctype == "CRM Call Log":
				# With the switch on, the peer inherits the lead denial on the Call Log.
				frappe.db.set_value(
					"CRM Tatva Automation", "Telephony::CRM Call Log::visibility", "enabled", 1
				)
				frappe.clear_cache()
				target = self._plant_call_log(lead, f"A10-{frappe.generate_hash(length=6)}")
			else:
				target = lead
			kwargs = _A10_KW[c.method](target)
			with set_user(user):
				if c.expected == "deny":
					with self.assertRaises(
						frappe.PermissionError,
						msg=f"A10 BYPASS: {user} ({c.principal}) reached {c.method} on a {c.doctype} it "
						"cannot read",
					):
						dispatch(c.method, **kwargs)
				elif c.method == "crm.integrations.api.get_recording_url":
					with self.assertRaisesRegex(frappe.DoesNotExistError, "Recording URL not found"):
						dispatch(c.method, **kwargs)  # native's own answer: only an admitted caller reaches it
				else:
					try:
						dispatch(c.method, **kwargs)
					except Exception as e:
						self.fail(f"A10 REGRESSION: authorized {user} could not complete {c.method}: {e!r}")
		finally:
			frappe.db.rollback(save_point=save_point)
			frappe.clear_cache()
