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
import frappe

from tatva_connect.lead import detail as lead_detail_mod
from tatva_connect.smartview import api as smartview_api
from tatva_connect.tests.authz import generator, grains, roster
from tatva_connect.tests.authz.base import AuthzTestCase, set_user
from tatva_connect.tests.authz.oracle import (
	native_doctype_capability,
	native_permitted_fields,
	native_visible_names,
	native_would_allow,
)
from tatva_connect.tests.authz.registry import cases as registry_cases

# The permlevel-1 grain fields — the A7 leak target and the A4/A7 oracle cross-check anchors.
GRAIN_FIELDNAMES = ("custom_vertical", "custom_group", "custom_current_program")


class TestRegistryCases(AuthzTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()  # comms-off interlock + class commit floor (rolled back per class)
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

	# ---- field-leak helpers (A4 / A7) -----------------------------------------------------------

	@staticmethod
	def _rendered_fieldnames_values(payload):
		"""(set of fieldnames, set of stringified values) rendered by lead_detail's section payload."""
		names, values = set(), set()
		for section in payload.get("sections", []):
			for f in section.get("fields", []):
				names.add(f.get("fieldname"))
				if f.get("value") is not None:
					values.add(str(f.get("value")))
		return names, values

	def _assert_grain_fields_not_in_oracle(self, c, user):
		"""The permlevel cross-check: a principal with no permlevel-1 read must NOT have the grain
		fields in native_permitted_fields (the ONLY sound field-leak oracle)."""
		permitted = native_permitted_fields(user, c.doctype)
		leaked = [fn for fn in GRAIN_FIELDNAMES if fn in permitted]
		self.assertEqual(
			leaked, [],
			"ORACLE: {0} natively has permlevel-1 read of {1} — the grain leak premise is void"
			.format(user, leaked),
		)

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
			self.skipTest("A1/A2 runner only handles surface 'list'; got {0}".format(c.surface))
		user = self._principal_user(c)
		target = self._resolve_target(c)
		if target is None:
			self.skipTest("no seeded {0} target for case {1}".format(c.target, c.id))
		visible = native_visible_names(user, c.doctype)  # the native ceiling (runs PQC)
		if c.expected == "deny":
			self.assertNotIn(
				target, visible,
				"A-LEAK: {0} ({1}) can see out-of-grain lead {2} — native list must not expose it"
				.format(user, c.principal, target),
			)
		else:
			self.assertIn(
				target, visible,
				"REGRESSION: {0} ({1}) cannot see its own in-grain lead {2}"
				.format(user, c.principal, target),
			)

	# ---- A4: grain-vs-role restriction wins (field; MUTATES → savepoint) -------------------------

	def test_A4_grain_vs_role_restriction(self):
		for c in registry_cases.cases_for("A4"):
			with self.subTest(case=c.id):
				self._run_a4_case(c)

	def _run_a4_case(self, c):
		if c.surface != "field":
			self.skipTest("A4 runner only handles surface 'field'; got {0}".format(c.surface))
		user = self._principal_user(c)
		target = self._resolve_target(c)
		if target is None:
			self.skipTest("no seeded {0} target for case {1}".format(c.target, c.id))
		role = roster.by_persona(c.principal)["roles"][0]  # the persona's primary desk role
		# Pick a real lead-surface catalog field this role would otherwise see, then restrict it.
		victim_key = self._a4_restrictable_key(user)
		if victim_key is None:
			self.skipTest("no entitled lead-detail catalog field to restrict for {0}".format(user))
		save_point = "authz_a4_{0}".format(c.id.replace("-", "_"))
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
				"A4: field {0} restricted for role {1} still renders in lead_detail for {2}"
				.format(victim_key, role, user),
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

	# ---- A7: permlevel field leak (field; oracle = permitted_fields) -----------------------------

	def test_A7_permlevel_field_leak(self):
		for c in registry_cases.cases_for("A7"):
			with self.subTest(case=c.id):
				self._run_a7_case(c)

	def _run_a7_case(self, c):
		if c.surface != "field":
			self.skipTest("A7 runner only handles surface 'field'; got {0}".format(c.surface))
		user = self._principal_user(c)
		target = self._resolve_target(c)
		if target is None:
			self.skipTest("no seeded {0} target for case {1}".format(c.target, c.id))
		# 1) Oracle cross-check: the grain fields are permlevel-1 → must NOT be in permitted_fields.
		self._assert_grain_fields_not_in_oracle(c, user)
		# 2) Surface check: the permlevel-1 grain field NAMES/VALUES must be absent from the render.
		grain = self._principal_grain(c.principal)
		grain_values = {grain["vertical"], grain["group"], grain["program"]} if grain else set()
		if "leaddetail" in c.id:
			with set_user(user):
				payload = lead_detail_mod.lead_detail(target)
			names, values = self._rendered_fieldnames_values(payload)
			leaked_names = [fn for fn in GRAIN_FIELDNAMES if fn in names]
			self.assertEqual(
				leaked_names, [],
				"A7: lead_detail exposed permlevel-1 grain field(s) {0} to {1}"
				.format(leaked_names, user),
			)
			leaked_vals = grain_values & values
			self.assertEqual(
				leaked_vals, set(),
				"A7: lead_detail leaked grain VALUE(s) {0} to {1}".format(leaked_vals, user),
			)
		elif "smartview" in c.id:
			self._assert_smartview_no_grain_leak(c, user, grain_values)
		else:
			self.skipTest("A7 case {0}: no field surface implemented for this id".format(c.id))

	def _assert_smartview_no_grain_leak(self, c, user, grain_values):
		"""get_data must not project permlevel-1 grain fields. Requires a saved Lead Smart View; if
		none exists for the principal, skip (the surface is user-authored, never seeded — invariant
		17 — so a fresh DB legitimately has no view to drive this)."""
		with set_user(user):
			views = smartview_api.get_smart_views()
			lead_view = next((v for v in views if v.get("base_object") == "Lead"), None)
			if lead_view is None:
				self.skipTest("A7 smartview: no Lead Smart View available to {0} (views are "
				              "user-authored, never seeded)".format(user))
			data = smartview_api.get_data(lead_view["name"], page_size=smartview_api.PAGE_MAX)
		col_fieldnames = {col.get("key") for col in data.get("columns", [])}
		leaked_cols = [fn for fn in GRAIN_FIELDNAMES if fn in col_fieldnames]
		self.assertEqual(
			leaked_cols, [],
			"A7: Smart View exposed permlevel-1 grain column(s) {0} to {1}".format(leaked_cols, user),
		)
		# No grain VALUE may appear in any projected row cell either.
		leaked = set()
		for row in data.get("rows", []):
			leaked |= grain_values & {str(v) for v in row.values() if v is not None}
		self.assertEqual(
			leaked, set(),
			"A7: Smart View leaked grain VALUE(s) {0} to {1}".format(leaked, user),
		)

	# ---- A6: bypass-write escalation (oracle = would_allow) --------------------------------------

	def test_A6_bypass_write_escalation(self):
		for c in registry_cases.cases_for("A6"):
			with self.subTest(case=c.id):
				self._run_a6_case(c)

	def _run_a6_case(self, c):
		user = self._principal_user(c)
		target = self._resolve_target(c)
		if target is None:
			self.skipTest("no seeded {0} target for case {1}".format(c.target, c.id))
		doc = frappe.get_doc(c.doctype, target)
		allowed = native_would_allow(user, c.doctype, c.action, doc)  # doc mandatory
		if c.expected == "deny":
			self.assertFalse(
				allowed,
				"A6 ESCALATION: {0} ({1}) may {2} out-of-grain {3}/{4} natively"
				.format(user, c.principal, c.action, c.doctype, target),
			)
		else:
			self.assertTrue(
				allowed,
				"A6 REGRESSION: {0} ({1}) cannot {2} in-scope {3}/{4}"
				.format(user, c.principal, c.action, c.doctype, target),
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
			self.skipTest("capability deny-sweep runner only judges DENY cases; {0} expects allow"
			              .format(c.id))
		user = self._principal_user(c)
		allowed = native_doctype_capability(user, c.doctype, c.action)
		self.assertFalse(
			allowed,
			"{0} BREACH: {1} ({2}) has {3} capability on {4}"
			.format(c.attack, user, c.principal, c.action, c.doctype),
		)
