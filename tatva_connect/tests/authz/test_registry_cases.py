# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Data-driven runner over the Tier-2 case registry (registry/cases.py).

One AuthzTestCase, one method per attack family. Each method iterates cases_for("Ax") and runs
every CaseSpec as an individual subTest (keyed on case.id) so the report keys per case. For each
case we: impersonate the principal, resolve a concrete target row from the 100 seeded leads by
their custom_* grain fields, exercise the real surface, and assert the verdict — always
cross-checked against the case's resolved_oracle() (the ceiling), never a hardcoded expectation.

Oracle ↔ surface pairing is the audit-critical bit (oracle.py docstring): list rows use
native_visible_names; permlevel field leaks use native_permitted_fields (has_permission is blind
to permlevel-1 — the grain product); row actions use native_would_allow / native_doctype_capability.

A surface with no implementation yet skipTest()s with a reason — never a silent pass.

MUTATION discipline (base.py): only A4 mutates shared state; it isolates with a named savepoint in
the subTest and rolls back to it, never a bare rollback (which would unwind the class seed floor).
"""
import re

import frappe

from tatva_connect.lead import detail as lead_detail_mod
from tatva_connect.tests.authz import generator, grains, roster
from tatva_connect.tests.authz.base import AuthzTestCase, set_user
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


class TestRegistryCases(AuthzTestCase):
	# A shared per-form-style submission sink for A14. Created ONCE (DDL) so a per-case scaffold's
	# implicit commit can't destroy each case's savepoint (intake is per-form now; there is no shared
	# staging doctype to borrow). Fields = the union the A14 cases stage; phone is Data so the fold's
	# own normaliser handles the raw value (no native Phone country-code gate in a fixture).
	A14_SINK = "Intake Authz A14 Sink"

	@classmethod
	def _ensure_a14_sink(cls):
		if not frappe.db.exists("DocType", cls.A14_SINK):
			frappe.get_doc({
				"doctype": "DocType", "name": cls.A14_SINK, "module": "Intake", "custom": 1,
				"autoname": "hash",
				"fields": [{"fieldname": f, "fieldtype": ft, "label": f} for f, ft in [
					("intake_form", "Data"), ("phone", "Data"), ("patient_name", "Data"),
					("state_manual", "Data"), ("doctor", "Data"), ("doctor_manual", "Data"),
					("remarks", "Small Text")]],
				"permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1}],
			}).insert(ignore_permissions=True)
			frappe.db.commit()  # DDL already committed implicitly; make the DocType row durable too

	@classmethod
	def setUpClass(cls):
		super().setUpClass()  # comms-off interlock + class commit floor (rolled back per class)
		cls._ensure_a14_sink()  # DDL BEFORE the (uncommitted) seed so its implicit commit can't leak it
		# Provisions roster + grain rules + partner mapping + 100 leads/tasks across the 5 grains.
		# commit=False: IntegrationTestCase rolls it all back per class. seed() fails LOUD
		# (assert_masters_exist throws) when a grain's CRM Vertical/Group/Program master is unseeded.
		cls.seeded = generator.seed(commit=False)

	# ---- target resolution: a concrete seeded lead for a case's `target` semantics ----------------

	def _principal_grain(self, principal):
		"""The (vertical, group, program) the persona's grain_key resolves to (None for non-grain)."""
		key = roster.by_persona(principal)["grain_key"]
		if not key:
			return None
		return next(g for g in grains.GRAINS if g["key"] == key)

	def _lead_in_grain(self, g):
		"""A seeded lead whose custom_* exactly match grain `g`. Fixture lookup (ignore_permissions),
		NOT an authz assertion — the assertion is what the SURFACE returns under the principal."""
		return frappe.db.get_value(
			"CRM Lead",
			{"custom_vertical": g["vertical"], "custom_group": g["group"],
			 "custom_current_program": g["program"], "lead_name": ["like", generator.TAG + "%"]},
			"name",
		)

	def _resolve_target(self, c):
		"""Map a CaseSpec.target token to a concrete seeded CRM Lead name (or None when N/A).
		  in_grain                    -> a lead in the principal's own grain
		  out_of_grain                -> a lead in a DIFFERENT vertical+group
		  same_program_diff_vertical  -> grain_5's lead (shares program 'InsideSales' with grain_4)
		"""
		own = self._principal_grain(c.principal)
		if c.target == "in_grain":
			return self._lead_in_grain(own) if own else None
		if c.target == "out_of_grain":
			other = next((g for g in grains.GRAINS
			              if not own or (g["vertical"], g["group"]) != (own["vertical"], own["group"])), None)
			return self._lead_in_grain(other) if other else None
		if c.target == "same_program_diff_vertical":
			# THE trap: grain_5 (GoodFlip/B2C/InsideSales) — same program name as grain_4, other axes.
			return self._lead_in_grain(self._principal_grain("grain_5"))
		return None

	@staticmethod
	def _principal_user(c):
		"""The login to impersonate — a roster email, or 'Guest' for the guest persona."""
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
				# grain_4 must get ZERO grain_5 leads despite the shared program 'InsideSales'.
				self._run_list_case(c)

	def _run_list_case(self, c):
		if c.surface != "list":
			self.skipTest(f"A1/A2 runner only handles surface 'list'; got {c.surface}")
		user = self._principal_user(c)
		target = self._resolve_target(c)
		if target is None:
			self.skipTest(f"no seeded {c.target} target for case {c.id}")
		visible = native_visible_names(user, c.doctype)  # the native ceiling (runs PQC)
		if c.expected == "deny":
			self.assertNotIn(
				target, visible,
				f"A-LEAK: {user} ({c.principal}) can see out-of-grain lead {target} — native list must not expose it"
				,
			)
		else:
			self.assertIn(
				target, visible,
				f"REGRESSION: {user} ({c.principal}) cannot see its own in-grain lead {target}"
				,
			)

	# ---- A4: grain-vs-role restriction wins (field; MUTATES → savepoint) -------------------------

	def test_A4_grain_vs_role_restriction(self):
		for c in registry_cases.cases_for("A4"):
			with self.subTest(case=c.id):
				self._run_a4_case(c)

	def _run_a4_case(self, c):
		if c.surface != "field":
			self.skipTest(f"A4 runner only handles surface 'field'; got {c.surface}")
		user = self._principal_user(c)
		target = self._resolve_target(c)
		if target is None:
			self.skipTest(f"no seeded {c.target} target for case {c.id}")
		role = roster.by_persona(c.principal)["roles"][0]  # the persona's primary desk role
		# Pick a real lead-surface catalog field this role would otherwise see, then restrict it.
		victim_key = self._a4_restrictable_key(user)
		if victim_key is None:
			self.skipTest(f"no entitled lead-detail catalog field to restrict for {user}")
		save_point = "authz_a4_{}".format(c.id.replace("-", "_"))
		frappe.db.savepoint(save_point)
		try:
			frappe.get_doc({
				"doctype": "CRM Lead Field Restriction", "role": role, "field": victim_key,
			}).insert(ignore_permissions=True)
			# entitled_grains / restrictions are request-cached — clear so the resolver re-reads.
			from tatva_connect.access import entitlement
			setattr(frappe.local, entitlement._RESTRICT_CACHE, {})
			with set_user(user):
				payload = lead_detail_mod.lead_detail(target)
			rendered_keys = {f.get("field_key")
			                 for s in payload.get("sections", []) for f in s.get("fields", [])}
			self.assertNotIn(
				victim_key, rendered_keys,
				f"A4: field {victim_key} restricted for role {role} still renders in lead_detail for {user}"
				,
			)
		finally:
			frappe.db.rollback(save_point=save_point)
			from tatva_connect.access import entitlement
			setattr(frappe.local, entitlement._RESTRICT_CACHE, {})

	@staticmethod
	def _a4_restrictable_key(user):
		"""A non-universal lead-detail catalog field_key the user is currently entitled to — the one
		we restrict and then prove disappears. None if the catalog yields nothing to restrict."""
		from tatva_connect.access import entitlement
		with set_user(user):
			catalog = {k: r for k, r in lead_detail_mod._catalog_rows().items()
			           if lead_detail_mod.is_profile_row(r)}
			visible = entitlement.resolve_fields(
				catalog, entitlement.entitled_grains(), frappe.get_roles())
		for key in visible:
			if key not in entitlement.UNIVERSAL_KEYS:
				return key
		return None

	# ---- A7: grain fields READ-allowed but EDIT-denied for a grain user --------------------------
	# Intended model (confirmed 2026-06-29): a Sales User SEES which grain a lead is in (read), but
	# only a manager / the assignment-rule stage may MOVE it (edit). So A7 asserts BOTH: read is
	# allowed (no false leak alarm), and an out-of-entitlement grain EDIT is rejected.

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
					self.skipTest(f"A7 runner: unhandled case shape {c.id}")

	def _run_a7_read_allowed(self, c):
		"""A grain user CAN natively read its own grain fields — this is intended, not a leak (the
		protection is on EDIT). Oracle = native_permitted_fields (permlevel-aware): the grain fields
		must be present for the grain user, who holds permlevel-1 read by design."""
		user = self._principal_user(c)
		permitted = native_permitted_fields(user, c.doctype)
		missing = [fn for fn in GRAIN_FIELDNAMES if fn not in permitted]
		self.assertEqual(
			missing, [],
			f"A7: grain user {user} is missing READ access to its own grain field(s) {missing} — read is the "
			"intended model (Sales User sees their grain)",
		)

	def _run_a7_edit_denied(self, c):
		"""A grain user must NOT move a lead OUT of its entitlement by editing a grain field. With the
		grain switch ON: vertical/group are permlevel-1 (structurally unwritable) and current_program
		(permlevel-0) is gated by the grain controller, which rejects an out-of-entitlement save.
		Switch OFF is the documented stock exposure (mirrors the child-visibility switches), so the
		assertion enables the switch first — exactly the prod-representative state. Mutates shared
		state, so it runs in a named savepoint per the base.py discipline (never a bare rollback)."""
		user = self._principal_user(c)
		own = self._principal_grain(c.principal)
		lead = self._lead_in_grain(own) if own else None
		if lead is None:
			self.skipTest(f"no seeded in-grain lead for case {c.id}")
		out_program = next(g["program"] for g in grains.GRAINS if g["program"] != own["program"])
		save_point = "authz_a7_{}".format(c.id.replace("-", "_"))
		frappe.db.savepoint(save_point)
		try:
			frappe.db.set_value("CRM Tatva Automation", _GRAIN_SWITCH, "enabled", 1)
			frappe.clear_cache()
			with set_user(user):
				doc = frappe.get_doc(c.doctype, lead)
				doc.custom_current_program = out_program  # move to a program outside entitlement
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
			self.skipTest(f"no seeded {c.target} target for case {c.id}")
		doc = frappe.get_doc(c.doctype, target)
		allowed = native_would_allow(user, c.doctype, c.action, doc)  # doc mandatory
		if c.expected == "deny":
			self.assertFalse(
				allowed,
				f"A6 ESCALATION: {user} ({c.principal}) may {c.action} out-of-grain {c.doctype}/{target} natively"
				,
			)
		else:
			self.assertTrue(
				allowed,
				f"A6 REGRESSION: {user} ({c.principal}) cannot {c.action} in-scope {c.doctype}/{target}"
				,
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
		"""Doctype-level DENY sweep — the only sanctioned doc=None oracle use (deny is the strongest
		verdict, no false negative). Used by A3 (no_role gains nothing) and A11 (cross-app leak)."""
		if c.expected != "deny":
			self.skipTest(f"capability deny-sweep runner only judges DENY cases; {c.id} expects allow"
			              )
		user = self._principal_user(c)
		allowed = native_doctype_capability(user, c.doctype, c.action)
		self.assertFalse(
			allowed,
			f"{c.attack} BREACH: {user} ({c.principal}) has {c.action} capability on {c.doctype}"
			,
		)

	# ---- A13: partner-mapping abuse (inner-core gate; drive AS the partner) -----------------------
	# The @_api endpoints SWALLOW exceptions into an error envelope (return None), so we drive the
	# INNER core functions directly, impersonating the partner — the proven pattern in
	# test_bypass_writes.py. The grain gate (resolve_lead's forced filter) raises BEFORE the
	# ignore_permissions save, so a grain_1 partner can never plant/overwrite a grain_3 row.

	def test_A13_partner_mapping_abuse(self):
		for c in registry_cases.cases_for("A13"):
			with self.subTest(case=c.id):
				self._run_a13_partner_case(c)

	@staticmethod
	def _plant_call_log(lead_name, external_id):
		"""Mint a CRM Call Log on `lead_name` carrying `external_id` (generator seeds no Call Logs).
		Runs as the default user (Administrator); the caller wraps it in a savepoint."""
		doc = frappe.new_doc("CRM Call Log")
		doc.id = f"AUTHZ-A13-{frappe.generate_hash(length=8)}"  # autoname is field:id
		doc.set("custom_external_id", external_id)
		doc.type = "Incoming"
		doc.status = "Completed"
		setattr(doc, "from", "")
		doc.to = ""
		doc.reference_doctype = "CRM Lead"
		doc.reference_docname = lead_name
		doc.insert(ignore_permissions=True)
		return doc.name

	def _run_a13_partner_case(self, c):
		from tatva_connect.api import partner_activity, partner_call
		from tatva_connect.api._base import _resolve_caller, find_by_external_id_scoped

		user = roster.email("partner")  # bound to grain_1 via the CRM Lead API Mapping seed()
		# resolve_lead raises the deliberately-generic not-found; widen the catch so a path change
		# can't turn a refusal into a false pass (same stance as test_bypass_writes).
		refusals = (frappe.DoesNotExistError, frappe.PermissionError, frappe.ValidationError)

		# A DISABLED mapping must deny the partner ALL access (no wrong-tenant attribution).
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
			self.skipTest(f"no seeded grain_3 (out-of-grain) lead for case {c.id}")

		# external_id is a PER-PARTNER namespace: a colliding id on a grain_3 row must NOT resolve.
		if c.id == "A13-partner-extid-collision-no-cross-tenant":
			save_point = "authz_a13_collision"
			external_id = "AUTHZ-A13-COLLIDE"
			frappe.db.savepoint(save_point)
			try:
				with set_user(user):
					_u, mp, is_sysmgr = _resolve_caller()
				self._plant_call_log(out_lead, external_id)  # as Administrator
				with set_user(user):
					hit = find_by_external_id_scoped(
						"CRM Call Log", "custom_external_id", external_id, mp, is_sysmgr)
				self.assertIsNone(
					hit,
					"A13 CROSS-TENANT: partner (grain_1) resolved a grain_3 Call Log by colliding "
					f"external_id {external_id} — find_by_external_id_scoped must grain-scope via the lead",
				)
			finally:
				frappe.db.rollback(save_point=save_point)
			return

		# The two out-of-grain CREATE cases: drive the inner core AS the partner against the grain_3
		# lead. resolve_lead's forced grain filter raises before any ignore_permissions save (no row
		# is written), so no savepoint is needed — only form_dict is restored.
		with set_user(user):
			_u, mp, is_sysmgr = _resolve_caller()
		original_form_dict = frappe.form_dict
		try:
			if c.id == "A13-partner-callog-out-of-grain-create":
				frappe.form_dict = frappe._dict({
					"lead": out_lead, "external_id": "AUTHZ-A13-CL", "direction": "Inbound",
					"from_number": "9990000010", "to_number": "9990000011",
				})
				core = lambda: partner_call._upsert_one(frappe.form_dict, mp, is_sysmgr)  # noqa: E731
			elif c.id == "A13-partner-activity-out-of-grain-create":
				frappe.form_dict = frappe._dict({
					"lead": out_lead, "external_id": "AUTHZ-A13-AC", "task_type": "__authz_probe__",
				})
				core = lambda: partner_activity._upsert_one(frappe.form_dict, mp, is_sysmgr)  # noqa: E731
			else:
				self.skipTest(f"A13 runner: unhandled case {c.id}")
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
	# A Guest web-form submit must not force routing across grain, grow a master, or write outside the
	# form's own lead. We build a forced-routing CRM Intake Form + a CRM Enrolment Submission sink,
	# then run intake._fold_submission_to_lead AS the Guest persona inside a savepoint.

	def test_A14_guest_intake(self):
		for c in registry_cases.cases_for("A14"):
			with self.subTest(case=c.id):
				self._run_a14_guest_intake(c)

	def _a14_intake_form(self, g, mappings):
		"""A forced-routing CRM Intake Form on grain `g` with the given field mappings, built
		IN-MEMORY and NOT inserted: the test calls _fold_submission_to_lead(sub, cfg) directly, which
		reads cfg's attributes + .mappings off the object — so an un-inserted contract is a complete
		substitute and no DDL touches the per-case savepoint. `source` is blank so no CRM Lead Source
		master is needed (a blank routing axis is not forced)."""
		doc = frappe.get_doc({
			"doctype": "CRM Intake Form",
			"form_name": f"authz-test-a14-{frappe.generate_hash(length=6)}",
			"enabled": 1,
			"custom_vertical": g["vertical"],
			"custom_group": g["group"],
			"custom_current_program": g["program"],
			"mappings": mappings,
		})
		doc.name = doc.form_name  # the fold stamps "Intake form: {cfg.name}" as provenance
		return doc

	def _a14_submission(self, cfg, values):
		"""A submission row on the shared A14 sink carrying only the fields the mappings read. The fold
		is driven explicitly with the in-memory cfg, so the sink is just a value carrier (DML inside
		the savepoint, no DDL). ignore_mandatory keeps the fixture minimal."""
		doc = frappe.get_doc({"doctype": self.A14_SINK, "intake_form": cfg.name, **values})
		doc.insert(ignore_permissions=True, ignore_mandatory=True)
		return doc

	def _a14_lead_for(self, phone):
		"""Find the lead the fold created/routed, by its phone (per-form sinks carry no `lead` field,
		so we can't read it back off the submission row)."""
		digits = re.sub(r"\D", "", phone)[-10:]
		name = frappe.db.get_value("CRM Lead", {"mobile_no": ["like", f"%{digits}%"]}, "name")
		return frappe.get_doc("CRM Lead", name) if name else None

	def _run_a14_guest_intake(self, c):
		from tatva_connect.intake import intake as intake_mod
		from tatva_connect.taxonomy.normalize import normalize_display

		g = grains.GRAINS[0]      # the FORM's grain (grain_1)
		foreign = grains.GRAINS[2]  # grain_3 — the smuggled foreign grain
		save_point = "authz_a14_{}".format(c.id.replace("-", "_"))
		frappe.db.savepoint(save_point)
		try:
			if c.id == "A14-guest-routing-forced":
				# Map free-text submission fields onto the lead's routing axes, then carry FOREIGN
				# values: the fold's is_sysmgr=False+mp must DROP them and force the form's grain.
				cfg = self._a14_intake_form(g, [
					{"source_field": "phone", "fieldtype": "Phone",
					 "target_table": "lead", "target_field": "mobile_no"},
					{"source_field": "patient_name", "target_table": "lead", "target_field": "custom_vertical"},
					{"source_field": "state_manual", "target_table": "lead", "target_field": "custom_group"},
					{"source_field": "doctor_manual", "target_table": "lead", "target_field": "custom_current_program"},
				])
				sub = self._a14_submission(cfg, {
					"phone": "+91 9990077001", "patient_name": foreign["vertical"],
					"state_manual": foreign["group"], "doctor_manual": foreign["program"],
				})
				with set_user("Guest"):
					intake_mod._fold_submission_to_lead(sub, cfg)
				lead = self._a14_lead_for("+91 9990077001")
				self.assertEqual(lead.custom_vertical, g["vertical"],
				                 f"A14: Guest smuggled vertical {foreign['vertical']} stuck on the lead")
				self.assertEqual(lead.custom_group, g["group"],
				                 f"A14: Guest smuggled group {foreign['group']} stuck on the lead")
				self.assertEqual(lead.custom_current_program, g["program"],
				                 f"A14: Guest smuggled program {foreign['program']} stuck on the lead")
				self.assertNotEqual(lead.custom_vertical, foreign["vertical"],
				                    "A14 ROUTING ESCALATION: foreign vertical was accepted from a Guest payload")

			elif c.id == "A14-guest-no-master-growth":
				# A manual field with master_doctype=CRM Side Effect Option -> _ensure_master. That
				# master is GROWABLE (not in _PICK_ONLY_MASTERS, not grain-scoped, single Data field),
				# so the ONLY thing that prevents a Guest from growing it is the Guest guard
				# (intake.py:355). The value is recorded as a note on the resolved lead.
				master = "CRM Side Effect Option"
				cfg = self._a14_intake_form(g, [
					{"source_field": "phone", "fieldtype": "Phone",
					 "target_table": "lead", "target_field": "mobile_no"},
					{"source_field": "doctor", "manual_field": "doctor_manual",
					 "master_doctype": master, "target_table": "note", "target_field": "option_name"},
				])
				typed = "Authz Guest Side Effect"
				canonical = normalize_display(typed)
				sub = self._a14_submission(cfg, {"phone": "+91 9990077002", "doctor_manual": typed})
				before = frappe.db.count(master)
				with set_user("Guest"):
					intake_mod._fold_submission_to_lead(sub, cfg)
				# (1) Guest did NOT grow the growable master, and the typed value is recorded as text.
				self.assertEqual(
					frappe.db.count(master), before,
					f"A14 MASTER GROWTH: a Guest submit grew {master} — the Guest guard (intake.py:355) is gone")
				lead = self._a14_lead_for("+91 9990077002")
				note = frappe.db.get_value(
					"FCRM Note",
					{"reference_doctype": "CRM Lead", "reference_docname": lead.name, "title": "option_name"},
					"content")
				self.assertEqual(note, canonical,
				                 f"A14: the typed value was not recorded as canonical text on the lead (note={note})")
				# (2) DIFFERENTIAL clincher (same savepoint): the SAME _ensure_master call as an AUTHED
				# user (the default Administrator) DOES grow the master by 1 — so the no-grow above is
				# Guest-specific, not a universal/pick-only block. Delete line 355 and (1) fails.
				authed_before = frappe.db.count(master)
				grown = intake_mod._ensure_master(master, "option_name", "Authz Authed Side Effect")
				self.assertEqual(
					frappe.db.count(master), authed_before + 1,
					f"A14: an authed _ensure_master did NOT grow {master} — the master must be growable "
					"for the Guest differential to mean anything")
				self.assertEqual(grown, normalize_display("Authz Authed Side Effect"))

			elif c.id == "A14-guest-note-scope":
				# A remarks -> note mapping: the fold's FCRM Note must reference ONLY its own lead.
				cfg = self._a14_intake_form(g, [
					{"source_field": "phone", "fieldtype": "Phone",
					 "target_table": "lead", "target_field": "mobile_no"},
					{"source_field": "remarks", "target_table": "note", "target_field": "Enrolment Remarks"},
				])
				sub = self._a14_submission(cfg, {"phone": "+91 9990077003", "remarks": "guest note body"})
				with set_user("Guest"):
					intake_mod._fold_submission_to_lead(sub, cfg)
				lead = self._a14_lead_for("+91 9990077003")
				refs = frappe.get_all(
					"FCRM Note", filters={"title": "Enrolment Remarks", "content": "guest note body"},
					pluck="reference_docname")
				self.assertTrue(refs, "A14: the Guest fold did not write its FCRM Note")
				self.assertTrue(
					all(r == lead.name for r in refs),
					f"A14 NOTE SCOPE: a Guest note referenced a lead other than the fold's own {lead.name}: {refs}")
			else:
				self.skipTest(f"A14 runner: unhandled case {c.id}")
		finally:
			frappe.db.rollback(save_point=save_point)
			frappe.clear_cache()

	# ---- A7 / A12: Smart View authoring clamp (drive upsert_view AS the grain user) ---------------
	# The Smart View write gate refuses to persist a cross-tenant view: an out-of-grain AXIS trips
	# _grains_from_axes (PermissionError) and an out-of-grain / non-catalog COLUMN trips
	# _validate_columns (ValidationError). Both raise BEFORE doc.save, so nothing is written (no
	# savepoint needed). Both are denials — the same fail-closed clamp from two angles.

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
			self.skipTest(f"smartview case {c.id} needs a grain principal")
		out = next((g for g in grains.GRAINS
		            if (g["vertical"], g["group"]) != (own["vertical"], own["group"])), None)
		if out is None:
			self.skipTest(f"no out-of-grain grain for case {c.id}")

		if c.attack == "A12":
			# Grain clamp: an out-of-grain AXIS is rejected by _grains_from_axes (PermissionError).
			view = {"label": "authz-clamp", "base_object": "Lead",
			        "vertical": out["vertical"], "group": out["group"], "program": out["program"]}
		else:
			# Column leak: the OWN (entitled) axis passes the clamp, but a column outside that grain's
			# catalog is rejected by _validate_columns (ValidationError) — fail-closed allowlist.
			view = {"label": "authz-col", "base_object": "Lead",
			        "vertical": own["vertical"], "group": own["group"], "program": own["program"],
			        "columns": ["lead:authz_out_of_grain_col"]}
		# _grains_from_axes raises PermissionError, _validate_columns raises ValidationError — both are
		# the clamp refusing; widen the catch so neither path can become a false pass.
		with set_user(user):
			with self.assertRaises(
				(frappe.PermissionError, frappe.ValidationError),
				msg=f"A SMART-VIEW LEAK: {user} ({c.principal}) persisted a cross-tenant view via {c.id}",
			):
				smartview_api.upsert_view(view)
