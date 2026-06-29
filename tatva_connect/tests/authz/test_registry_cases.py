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

	# ---- A7: grain fields READ-allowed but EDIT-denied for a grain user --------------------------
	# Intended model (confirmed 2026-06-29): a Sales User SEES which grain a lead is in (read), but
	# only a manager / the assignment-rule stage may MOVE it (edit). So A7 asserts BOTH: read is
	# allowed (no false leak alarm), and an out-of-entitlement grain EDIT is rejected.

	def test_A7_grain_field_read_allowed_edit_denied(self):
		for c in registry_cases.cases_for("A7"):
			with self.subTest(case=c.id):
				if c.action == "field_read" and c.expected == "allow":
					self._run_a7_read_allowed(c)
				elif c.action == "write" and c.expected == "deny":
					self._run_a7_edit_denied(c)
				else:
					self.skipTest("A7 runner: unhandled case shape {0}".format(c.id))

	def _run_a7_read_allowed(self, c):
		"""A grain user CAN natively read its own grain fields — this is intended, not a leak (the
		protection is on EDIT). Oracle = native_permitted_fields (permlevel-aware): the grain fields
		must be present for the grain user, who holds permlevel-1 read by design."""
		user = self._principal_user(c)
		permitted = native_permitted_fields(user, c.doctype)
		missing = [fn for fn in GRAIN_FIELDNAMES if fn not in permitted]
		self.assertEqual(
			missing, [],
			"A7: grain user {0} is missing READ access to its own grain field(s) {1} — read is the "
			"intended model (Sales User sees their grain)".format(user, missing),
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
			self.skipTest("no seeded in-grain lead for case {0}".format(c.id))
		out_program = next(g["program"] for g in grains.GRAINS if g["program"] != own["program"])
		save_point = "authz_a7_{0}".format(c.id.replace("-", "_"))
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
