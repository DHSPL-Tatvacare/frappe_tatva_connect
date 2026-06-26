# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Planted violations — the negative controls that prove the suite can go RED (TESTS.md §9).

A green suite proves nothing unless we prove it can catch a real bug. So for EVERY attack vector
(A1..A13) we plant at least one deliberate violation and confirm the matching detector flags it.
`test_self_validation.py` runs each plant in a SAVEPOINT, scores it into a `Confusion`, and asserts
recall == 1.0 — a planted bug that stays green is a False Negative = a build failure.

Two kinds of mutation (mirrors §9):

  (a) DATA mutation — seed a condition that IS a violation and assert the relevant ORACLE flags it.
      e.g. share an out-of-grain lead to a grain user (A1); grant permlevel-1 read so a grain field
      leaks (A7); open a sensitive doctype to a cross-app role via a Custom DocPerm (A11). The
      detector is oracle-based, so it stays honest: the same native engine the real test trusts now
      reports MORE than the clean baseline, and we confirm that delta is visible.

  (b) CODE mutation that has no data analogue — some bugs live only in app code (e.g. "if the grain
      matcher keyed on program ALONE, grain_4 would see grain_5"). We cannot edit app code from a
      test. Where the bug's EFFECT can be simulated with data we do that (A2 shares grain_5's lead to
      grain_4 — exactly the row a program-only match would leak). Where it genuinely cannot, the spec
      is marked `untestable_without_code_mutation` with a reason; the self-validation REPORTS those
      as build-visible warnings and still requires >=1 detectable mutation per vector — so recall
      can never reach 1.0 by quietly excluding an untested vector (audit M2).

Plant contract: `plant()` mutates the DB (inside a caller-owned savepoint) to create the known-bad
condition and returns a small context dict; `detect(ctx)` returns True iff the violation is
detectable (i.e. the suite would go red). Both are pure of commits. The caller clears the request
caches between plant and detect (the entitlement/restriction resolvers memoise per request).

Every spec is a dict:
    attack   — ATTACKS key (A1..A13)
    id       — stable, unique
    english  — one-line description of the planted violation
    plant    — callable() -> ctx   (None when untestable)
    detect   — callable(ctx) -> bool  (None when untestable)
    expected_detector — which oracle/test SHOULD flag it (for the report)
    untestable_without_code_mutation — reason string, or None
"""
import frappe

from tatva_connect.tests.authz import grains, roster
from tatva_connect.tests.authz.generator import TAG
from tatva_connect.tests.authz.oracle import (
	native_can_read_row,
	native_doctype_capability,
	native_permitted_fields,
	native_visible_names,
	native_would_allow,
)

# Grain fields are permlevel-1 on CRM Lead; a user without permlevel-1 read must never receive them.
PERMLEVEL_FIELDS = ("custom_vertical", "custom_group", "custom_current_program")
# The CRM Lead API Field catalog keys for those grain fields (Field Restriction targets a catalog row).
RESTRICTABLE_FIELD = "custom_vertical"


# ---- helpers --------------------------------------------------------------------------------------

def _lead_in_grain(idx):
	"""A seeded CRM Lead name belonging to GRAINS[idx] (generator spreads 100 leads round-robin)."""
	g = grains.GRAINS[idx]
	name = frappe.db.get_value(
		"CRM Lead",
		{"lead_name": ["like", TAG + "%"], "custom_vertical": g["vertical"],
		 "custom_group": g["group"], "custom_current_program": g["program"]},
		"name",
	)
	if not name:
		frappe.throw("mutation precondition: no seeded lead for grain {0} — run generator.seed() "
		             "in setUpClass first".format(g["key"]))
	return name


def _task_for_lead(lead_name):
	"""A seeded CRM Task whose parent is `lead_name`."""
	return frappe.db.get_value(
		"CRM Task", {"reference_doctype": "CRM Lead", "reference_docname": lead_name}, "name")


def _clear_access_caches():
	"""Drop the per-request memo buckets the entitlement/restriction resolvers use, so a detector
	reading after a plant sees the planted row (request_cache lives on frappe.local)."""
	for bucket in ("tatva_connect:entitled_grains", "tatva_connect:field_restrictions",
	               "tatva_connect:visible_parents"):
		if hasattr(frappe.local, bucket):
			delattr(frappe.local, bucket)


def _grant_docshare(doctype, name, user, read=1, write=0):
	"""Native DocShare grant — the ONLY layer (besides role perms) that can WIDEN access. Sharing an
	out-of-grain row to a grain user is a real over-grant the row oracle must surface."""
	frappe.share.add(doctype, name, user, read=read, write=write, flags={"ignore_share_permission": True})


# ---- plant/detect pairs -------------------------------------------------------------------------
# Each builder returns (plant, detect). plant() creates the known-bad; detect(ctx) -> was it caught?

def _share_out_of_grain(grain_idx, other_idx):
	"""DATA: share an other-grain lead to a grain user. The row oracle (get_list, honours shares)
	must now list a lead the grain user should never own — a horizontal leak made visible."""
	def plant():
		user = roster.email("grain_{0}".format(grain_idx + 1))
		leaked = _lead_in_grain(other_idx)
		_grant_docshare("CRM Lead", leaked, user)
		return {"user": user, "leaked": leaked}

	def detect(ctx):
		_clear_access_caches()
		return ctx["leaked"] in native_visible_names(ctx["user"], "CRM Lead")

	return plant, detect


def _open_doctype_to_role(doctype, role):
	"""DATA: add a Custom DocPerm granting `role` read on a doctype outside its remit. The capability
	oracle must now report the role CAN read it — the cross-app / escalation leak."""
	def plant():
		probe = "authz.mut.{0}@example.test".format(frappe.scrub(role))
		if not frappe.db.exists("User", probe):
			frappe.get_doc({
				"doctype": "User", "email": probe, "first_name": "mut-{0}".format(role),
				"user_type": "System User", "send_welcome_email": 0, "roles": [{"role": role}],
			}).insert(ignore_permissions=True)
		# add_permission writes a Custom DocPerm; reset_perms is the rollback the savepoint handles.
		frappe.permissions.add_permission(doctype, role, permlevel=0)
		frappe.permissions.update_permission_property(doctype, role, 0, "read", 1)
		frappe.clear_cache(doctype=doctype)
		return {"probe": probe, "doctype": doctype}

	def detect(ctx):
		return native_doctype_capability(ctx["probe"], ctx["doctype"], "read")

	return plant, detect


def _grant_write_to_role(doctype, role):
	"""DATA: grant `role` write on a doctype it must never write — escalation/bypass effect, judged
	on a CONCRETE doc by native_would_allow (doc mandatory; doc=None would mask row-level escalation)."""
	def plant():
		probe = "authz.mut.{0}.w@example.test".format(frappe.scrub(role))
		if not frappe.db.exists("User", probe):
			frappe.get_doc({
				"doctype": "User", "email": probe, "first_name": "mutw-{0}".format(role),
				"user_type": "System User", "send_welcome_email": 0, "roles": [{"role": role}],
			}).insert(ignore_permissions=True)
		frappe.permissions.add_permission(doctype, role, permlevel=0)
		frappe.permissions.update_permission_property(doctype, role, 0, "write", 1)
		frappe.permissions.update_permission_property(doctype, role, 0, "read", 1)
		frappe.clear_cache(doctype=doctype)
		lead = _lead_in_grain(0)
		return {"probe": probe, "doctype": doctype, "doc": frappe.get_doc(doctype, lead)}

	def detect(ctx):
		return native_would_allow(ctx["probe"], ctx["doctype"], "write", ctx["doc"])

	return plant, detect


def _grant_permlevel1_read(role):
	"""DATA: grant `role` permlevel-1 READ on CRM Lead. The field oracle (permlevel-aware) must now
	include the grain fields in the role-holder's permitted set — the permlevel field leak (audit C1).
	This simulates the EFFECT of a render surface that bypasses permlevel: native then allows it."""
	def plant():
		probe = "authz.mut.{0}.pl1@example.test".format(frappe.scrub(role))
		if not frappe.db.exists("User", probe):
			frappe.get_doc({
				"doctype": "User", "email": probe, "first_name": "mutpl-{0}".format(role),
				"user_type": "System User", "send_welcome_email": 0, "roles": [{"role": role}],
			}).insert(ignore_permissions=True)
		# Clean baseline FIRST (before the grant) — the grain fields must NOT be present here.
		baseline = native_permitted_fields(probe, "CRM Lead")
		frappe.permissions.add_permission("CRM Lead", role, permlevel=1)
		frappe.permissions.update_permission_property("CRM Lead", role, 1, "read", 1)
		frappe.clear_cache(doctype="CRM Lead")
		return {"probe": probe, "baseline_had": [f for f in PERMLEVEL_FIELDS if f in baseline]}

	def detect(ctx):
		fields = native_permitted_fields(ctx["probe"], "CRM Lead")
		# Detected iff a grain field that was NOT in the clean baseline is now exposed.
		return any(f in fields and f not in ctx["baseline_had"] for f in PERMLEVEL_FIELDS)

	return plant, detect


def _remove_field_restriction_effect(role):
	"""DATA (A4 grain-vs-role): a CRM Lead Field Restriction must HIDE a field from a role even when
	the grain would show it. The violation we plant is the restriction's ABSENCE while the grain
	would show the field — i.e. the resolver returns a field the restriction was meant to hide.
	Detector mirrors resolve_fields: with NO restriction row, the field survives -> leak detectable."""
	from tatva_connect.access import entitlement

	def plant():
		# Ensure there is NO restriction for this role/field (the bug: restriction removed/never set).
		for n in frappe.get_all("CRM Lead Field Restriction",
		                        filters={"role": role, "field": RESTRICTABLE_FIELD}, pluck="name"):
			frappe.delete_doc("CRM Lead Field Restriction", n, ignore_permissions=True, force=True)
		return {"role": role}

	def detect(ctx):
		_clear_access_caches()
		# A field the role-restriction SHOULD hide survives resolve_fields when the restriction is
		# absent. catalog_rows keyed by field_key; the grain field is in-grain so field_in_grains
		# passes -> the only thing that could hide it is the restriction. Absent -> it leaks.
		catalog = {"lead:" + RESTRICTABLE_FIELD: {"grain_vertical": "", "grain_group": "",
		                                           "grain_program": ""}}
		resolved = entitlement.resolve_fields(catalog, grains.ALL_GRAINS, [ctx["role"]])
		return ("lead:" + RESTRICTABLE_FIELD) in resolved

	return plant, detect


def _share_child_parent(grain_idx, other_idx):
	"""DATA (A8 orphan/forged-parent flavour & child read): share an other-grain lead's child Task to
	a grain user via DocShare. native_can_read_row (PQC-honouring) must now report the child readable
	— a child becomes visible it should not. Switch-agnostic: a DocShare grant widens read on any
	path, which is exactly the over-grant the can_read_row oracle exists to catch."""
	def plant():
		user = roster.email("grain_{0}".format(grain_idx + 1))
		leaked_lead = _lead_in_grain(other_idx)
		task = _task_for_lead(leaked_lead)
		if not task:
			frappe.throw("mutation precondition: no seeded task for the out-of-grain lead")
		_grant_docshare("CRM Task", task, user, read=1)
		return {"user": user, "task": task}

	def detect(ctx):
		_clear_access_caches()
		return native_can_read_row(ctx["user"], "CRM Task", ctx["task"])

	return plant, detect


def _partner_write_out_of_grain():
	"""DATA (A13/A6 partner abuse): grant the partner user write on an out-of-grain lead via DocShare.
	The partner's mapping is grain_1; sharing a TatvaPractice lead with write is a cross-tenant write
	grant. native_would_allow on that concrete doc must now report write allowed — the attribution
	abuse / bypass-write effect made visible on a real row."""
	def plant():
		user = roster.email("partner")
		leaked = _lead_in_grain(2)  # TatvaPractice/India/FieldSales — outside the partner's grain_1
		_grant_docshare("CRM Lead", leaked, user, read=1, write=1)
		return {"user": user, "doc": frappe.get_doc("CRM Lead", leaked)}

	def detect(ctx):
		return native_would_allow(ctx["user"], "CRM Lead", "write", ctx["doc"])

	return plant, detect


# ---- the registry of planted violations ---------------------------------------------------------
# AT LEAST ONE per attack vector. Untestable-without-code-mutation vectors are declared explicitly
# (never silently skipped) so the self-validation can surface them as build warnings.

def _build_mutations():
	a1p, a1d = _share_out_of_grain(3, 0)           # grain_4 user, share a grain_1 lead
	a2p, a2d = _share_out_of_grain(3, 4)           # grain_4 user, share a grain_5 (shared-program) lead
	a3p, a3d = _grant_write_to_role("Purchase Master Manager")  # cross-app role gains CRM Lead write
	a4p, a4d = _remove_field_restriction_effect("Sales User")
	a5p, a5d = _share_out_of_grain(0, 3)           # roll-up flavour: a non-report grain leaks in
	a6p, a6d = _grant_write_to_role("Sales User")  # a write grant the bypass path would honour
	a7p, a7d = _grant_permlevel1_read("Sales User")
	a8p, a8d = _share_child_parent(3, 0)
	a10p, a10d = _grant_write_to_role("WhatsApp User")  # wrapped-method ceiling = native_would_allow
	a11p, a11d = _open_doctype_to_role("Contact", "Purchase Master Manager")
	a12p, a12d = _share_out_of_grain(4, 2)         # DocShare over-grant (the share IS the fence abuse)
	a13p, a13d = _partner_write_out_of_grain()

	return [
		{"attack": "A1", "id": "MUT-A1-share-other-grain-lead",
		 "english": "grain_4 user is DocShared a grain_1 lead -> appears in their get_list (horizontal leak)",
		 "plant": a1p, "detect": a1d, "expected_detector": "oracle.native_visible_names",
		 "untestable_without_code_mutation": None},

		{"attack": "A2", "id": "MUT-A2-share-shared-program-lead",
		 "english": "grain_4 user is DocShared a grain_5 lead — the SAME-program/diff-vertical row a "
		            "program-only grain match would wrongly leak (simulates that code bug's effect)",
		 "plant": a2p, "detect": a2d, "expected_detector": "oracle.native_visible_names",
		 "untestable_without_code_mutation": None},

		{"attack": "A3", "id": "MUT-A3-crossapp-role-gains-lead-write",
		 "english": "Purchase Master Manager (cross-app role) granted CRM Lead write — vertical escalation",
		 "plant": a3p, "detect": a3d, "expected_detector": "oracle.native_would_allow",
		 "untestable_without_code_mutation": None},

		{"attack": "A4", "id": "MUT-A4-field-restriction-removed",
		 "english": "CRM Lead Field Restriction for the role is absent -> resolve_fields returns a "
		            "field the restriction must hide (grain-vs-role: restriction must win)",
		 "plant": a4p, "detect": a4d, "expected_detector": "entitlement.resolve_fields",
		 "untestable_without_code_mutation": None},

		{"attack": "A5", "id": "MUT-A5-rollup-overwiden-share",
		 "english": "a grain_1 user is DocShared an out-of-grain (grain_4) lead — stands in for a "
		            "reports_to roll-up that over-widens beyond the union of reports' grains",
		 "plant": a5p, "detect": a5d, "expected_detector": "oracle.native_visible_names",
		 "untestable_without_code_mutation":
			 "Full reports_to roll-up over-widening (cycle/depth bypass) needs a User.reports_to "
			 "field + a code path that walks it; stock CRM has no reports_to column so the true "
			 "roll-up bug can't be exercised here. We test the EFFECT (an extra-grain row becomes "
			 "visible) via a share; the cycle/depth logic itself needs a code-level mutation."},

		{"attack": "A6", "id": "MUT-A6-bypass-write-grant",
		 "english": "Sales User granted CRM Lead write on an out-of-grain doc — the ceiling an "
		            "ignore_permissions bypass path must not exceed (would_allow on a concrete doc)",
		 "plant": a6p, "detect": a6d, "expected_detector": "oracle.native_would_allow",
		 "untestable_without_code_mutation": None},

		{"attack": "A7", "id": "MUT-A7-permlevel1-field-leak",
		 "english": "Sales User granted permlevel-1 read on CRM Lead -> grain fields enter the "
		            "permitted-field set (the permlevel field leak, audit C1)",
		 "plant": a7p, "detect": a7d, "expected_detector": "oracle.native_permitted_fields",
		 "untestable_without_code_mutation": None},

		{"attack": "A8", "id": "MUT-A8-child-share-out-of-grain",
		 "english": "grain_4 user is DocShared an out-of-grain lead's child CRM Task -> "
		            "native_can_read_row reports it readable (child visible via a forged/extra grant)",
		 "plant": a8p, "detect": a8d, "expected_detector": "oracle.native_can_read_row",
		 "untestable_without_code_mutation": None},

		{"attack": "A9", "id": "MUT-A9-switch-off-exposure",
		 "english": "child-doctype visibility switch OFF = stock CRM (no row scoping) — the "
		            "documented exposure delta",
		 "plant": None, "detect": None, "expected_detector": "oracle.native_visible_names",
		 "untestable_without_code_mutation":
			 "Switch-OFF is the DESIGNED stock-CRM fallback (visibility.scoped_pqc returns '' when "
			 "the operator switch is off), not a code bug, and the native oracle agrees with stock "
			 "CRM in that state (no escalation BEYOND native to detect). A meaningful negative "
			 "control here is a Playwright/UI check that the switch-OFF delta is surfaced to the "
			 "operator, not an in-process oracle assertion — recorded, not silently skipped."},

		{"attack": "A10", "id": "MUT-A10-wrapped-method-ceiling",
		 "english": "WhatsApp User granted CRM Lead write — the raw native call's ceiling that a "
		            "wrapped-method must not exceed (would_allow on a concrete doc)",
		 "plant": a10p, "detect": a10d, "expected_detector": "oracle.native_would_allow",
		 "untestable_without_code_mutation":
			 "The actual 11 method WRAPPERS are exercised by test_bypass_writes / the AST lock "
			 "(test_no_perm_bypass). Here we plant the data ceiling the wrapper must respect; the "
			 "wrapper-vs-raw differential itself is a code-path test, not an oracle data plant."},

		{"attack": "A11", "id": "MUT-A11-crossapp-contact-read-open",
		 "english": "Custom DocPerm opens Contact read to Purchase Master Manager -> cross-app leak "
		            "(the original VAPT regression class)",
		 "plant": a11p, "detect": a11d, "expected_detector": "oracle.native_doctype_capability",
		 "untestable_without_code_mutation": None},

		{"attack": "A12", "id": "MUT-A12-docshare-over-grant",
		 "english": "a DocShare grants a grain user an out-of-grain lead — a share/fence over-grant "
		            "the row oracle surfaces",
		 "plant": a12p, "detect": a12d, "expected_detector": "oracle.native_visible_names",
		 "untestable_without_code_mutation": None},

		{"attack": "A13", "id": "MUT-A13-partner-cross-tenant-write",
		 "english": "the partner (mapped to grain_1) is DocShared write on a TatvaPractice lead -> "
		            "cross-tenant write grant (attribution abuse, invariant 16)",
		 "plant": a13p, "detect": a13d, "expected_detector": "oracle.native_would_allow",
		 "untestable_without_code_mutation": None},
	]


MUTATIONS = _build_mutations()


def testable():
	"""Mutations that plant a detectable bug (have plant+detect)."""
	return [m for m in MUTATIONS if m["plant"] is not None and m["detect"] is not None]


def untestable():
	"""Mutations declared untestable-without-code-mutation (surfaced as build warnings)."""
	return [m for m in MUTATIONS if m["untestable_without_code_mutation"]]
