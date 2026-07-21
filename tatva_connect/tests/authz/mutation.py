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
	native_http_verdict,
	native_permitted_fields,
	native_visible_names,
	native_would_allow,
)

# custom_vertical / custom_group are permlevel-1 on CRM Lead (verified live 2026-06-29);
# custom_current_program is permlevel-0, so it is NOT a permlevel-leak target. A7 tests the two
# genuinely permlevel-1 fields. (A4 resolves its own target from the live catalog — see _restrictable_key.)


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
		frappe.throw("mutation precondition: no seeded lead for grain {} — run generator.seed() "
		             "in setUpClass first".format(g["key"]))
	return name


def _task_for_lead(lead_name):
	"""A seeded CRM Task whose parent is `lead_name`."""
	return frappe.db.get_value(
		"CRM Task", {"reference_doctype": "CRM Lead", "reference_docname": lead_name}, "name")


def _clear_access_caches():
	"""Drop the per-request memo buckets the entitlement/restriction resolvers use, so a detector
	reading after a plant sees the planted row (request_cache lives on frappe.local)."""
	# The two contract-tick buckets are here because grain membership moved onto the contract (Phase 5/9) — a plant that ticks a field is invisible to its detector without them.
	for bucket in ("tatva_connect:entitled_grains", "tatva_connect:field_restrictions",
	               "tatva_connect:visible_parents", "tatva_connect:internal_contract_ticks",
	               "tatva_connect:internal_universal_fields"):
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
		user = roster.email(f"grain_{grain_idx + 1}")
		leaked = _lead_in_grain(other_idx)
		_grant_docshare("CRM Lead", leaked, user)
		return {"user": user, "leaked": leaked}

	def detect(ctx):
		_clear_access_caches()
		return ctx["leaked"] in native_visible_names(ctx["user"], "CRM Lead")

	return plant, detect


def _open_doctype_to_role(doctype, role, ptype="read"):
	"""DATA: add a Custom DocPerm granting `role` `ptype` on a doctype outside its remit. The capability
	oracle (native_doctype_capability, doc=None) must now report the role HAS that capability — the
	cross-app (A11) / vertical-escalation (A3) leak.

	doc=None is the SOUND oracle for a doctype-capability grant: has_permission with doc=None evaluates
	role perms only (verified in permissions.py — the controller `has_permission` hooks need a doc), so a
	layer-2 grant is visible here. (On a CONCRETE doc the deny-only layer-4 org_hierarchy hook would mask
	this same grant — which is exactly why the old would_allow detector stayed green: a false negative.)"""
	def plant():
		probe = f"authz.mut.{frappe.scrub(role)}.{frappe.scrub(doctype)}.{ptype}@example.test"
		if not frappe.db.exists("User", probe):
			frappe.get_doc({
				"doctype": "User", "email": probe, "first_name": f"mut-{role}",
				"user_type": "System User", "send_welcome_email": 0, "roles": [{"role": role}],
			}).insert(ignore_permissions=True)
		# add_permission writes a Custom DocPerm; reset_perms is the rollback the savepoint handles.
		frappe.permissions.add_permission(doctype, role, permlevel=0)
		frappe.permissions.update_permission_property(doctype, role, 0, ptype, 1)
		if ptype != "read":  # write/create/delete capability implies the doctype must be readable too
			frappe.permissions.update_permission_property(doctype, role, 0, "read", 1)
		frappe.clear_cache(doctype=doctype)
		return {"probe": probe, "doctype": doctype, "ptype": ptype}

	def detect(ctx):
		return native_doctype_capability(ctx["probe"], ctx["doctype"], ctx["ptype"])

	return plant, detect


def _share_doc_write(role, lead_idx):
	"""DATA (A6/A10): DocShare WRITE a concrete lead to a probe holding `role`. This is the correct
	would_allow negative control — verified in permissions.py: has_permission on a doc OR-s in a
	DocShare (layer 6) only when role+controller perm is falsy, and the share bypasses the deny-only
	layer-4 org_hierarchy hook, so native_would_allow on that concrete doc now returns True. It is the
	real row-level write ceiling an ignore_permissions bypass path must never exceed.

	NB: a bare role-perm grant (the old detector) does NOT surface here — CRM Lead's layer-4
	has_lead_permission hook denies an unassigned probe, AND-composed, masking the grant. That mismatch
	was the false negative this fixes."""
	def plant():
		probe = f"authz.mut.{frappe.scrub(role)}.shw@example.test"
		if not frappe.db.exists("User", probe):
			frappe.get_doc({
				"doctype": "User", "email": probe, "first_name": f"shw-{role}",
				"user_type": "System User", "send_welcome_email": 0, "roles": [{"role": role}],
			}).insert(ignore_permissions=True)
		lead = _lead_in_grain(lead_idx)
		_grant_docshare("CRM Lead", lead, probe, read=1, write=1)
		return {"probe": probe, "doc": frappe.get_doc("CRM Lead", lead)}

	def detect(ctx):
		return native_would_allow(ctx["probe"], "CRM Lead", "write", ctx["doc"])

	return plant, detect


def _grant_permlevel1_read():
	"""DATA (A7): grant a probe permlevel-1 READ on CRM Lead — custom_vertical / custom_group are
	permlevel-1 (verified live; custom_current_program is permlevel-0, hence NOT a permlevel target).
	The field oracle (permlevel-aware get_permitted_fields) must then include them — the permlevel field
	leak (audit C1), the EFFECT of a render surface that bypasses permlevel.

	The probe holds a THROWAWAY role with ONLY permlevel-0 read, so the pl1 fields are absent at
	baseline and the grant produces a real delta. A grain Sales User can NOT be the probe: on this site
	it already holds permlevel-1 read (a Custom DocPerm), so granting it again is a no-op — exactly the
	wrong-principal mistake that made the old detector a false negative."""
	PL1_FIELDS = ("custom_vertical", "custom_group")
	role = "Authz Mut PL0 Probe Role"

	def plant():
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}
			               ).insert(ignore_permissions=True)
		probe = "authz.mut.pl0probe@example.test"
		if not frappe.db.exists("User", probe):
			frappe.get_doc({
				"doctype": "User", "email": probe, "first_name": "pl0probe",
				"user_type": "System User", "send_welcome_email": 0, "roles": [{"role": role}],
			}).insert(ignore_permissions=True)
		# permlevel-0 read only → the doctype is readable but the pl1 grain fields are NOT (clean baseline).
		frappe.permissions.add_permission("CRM Lead", role, permlevel=0)
		frappe.permissions.update_permission_property("CRM Lead", role, 0, "read", 1)
		frappe.clear_cache(doctype="CRM Lead")
		baseline = native_permitted_fields(probe, "CRM Lead")
		baseline_had = [f for f in PL1_FIELDS if f in baseline]
		# the leak: grant permlevel-1 read → the pl1 grain fields must now enter the permitted set.
		frappe.permissions.add_permission("CRM Lead", role, permlevel=1)
		frappe.permissions.update_permission_property("CRM Lead", role, 1, "read", 1)
		frappe.clear_cache(doctype="CRM Lead")
		return {"probe": probe, "baseline_had": baseline_had}

	def detect(ctx):
		fields = native_permitted_fields(ctx["probe"], "CRM Lead")
		# Detected iff a permlevel-1 grain field that was NOT in the clean baseline is now exposed.
		return any(f in fields and f not in ctx["baseline_had"] for f in PL1_FIELDS)

	return plant, detect


def _remove_field_restriction_effect(role):
	"""DATA (A4 grain-vs-role): a CRM Lead Field Restriction must HIDE a field from a role even when
	the grain would show it. The violation we plant is the restriction's ABSENCE while the grain
	would show the field — i.e. the resolver returns a field the restriction was meant to hide.
	Detector mirrors resolve_fields: with NO restriction row, the field survives -> leak detectable."""
	from tatva_connect.access import entitlement

	def _restrictable_key():
		"""A REAL catalogued lead field the restriction can target. CRM Lead Field Restriction.field is a Link to CRM Lead API Field, so an uncatalogued fieldname can never be restricted; a contract-universal field is skipped so the case exercises a grain-specific one."""
		for row in frappe.get_all("CRM Lead API Field", filters={"section": "lead"},
		                          fields=["field_key", "fieldname"], order_by="field_key asc"):
			if not entitlement.is_universal_field(row.field_key):
				return row.field_key, row.fieldname
		return None, None

	def _resolved_has(key, fieldname, the_role):
		_clear_access_caches()
		catalog = {key: {"field_key": key, "section": "lead", "fieldname": fieldname}}
		return key in entitlement.resolve_fields(catalog, entitlement.ALL_GRAINS, [the_role])

	def plant():
		key, fieldname = _restrictable_key()
		# BASELINE FIRST: prove the restriction genuinely hides the field, so a detection can only come from removing it — never from a no-op that matched nothing.
		if not frappe.db.exists("CRM Lead Field Restriction", {"role": role, "field": key}):
			frappe.get_doc({"doctype": "CRM Lead Field Restriction", "role": role, "field": key}
			               ).insert(ignore_permissions=True)
		baseline_hidden = not _resolved_has(key, fieldname, role)
		# The violation: the restriction is removed, so the field the role must never see survives.
		for n in frappe.get_all("CRM Lead Field Restriction",
		                        filters={"role": role, "field": key}, pluck="name"):
			frappe.delete_doc("CRM Lead Field Restriction", n, ignore_permissions=True, force=True)
		return {"role": role, "key": key, "fieldname": fieldname, "baseline_hidden": baseline_hidden}

	def detect(ctx):
		# Detected iff the field was genuinely hidden at baseline and leaks once the restriction is gone.
		return _resolved_has(ctx["key"], ctx["fieldname"], ctx["role"]) and ctx["baseline_hidden"]

	return plant, detect


def _share_child_parent(grain_idx, other_idx):
	"""DATA (A8 orphan/forged-parent flavour & child read): share an other-grain lead's child Task to
	a grain user via DocShare. native_can_read_row (PQC-honouring) must now report the child readable
	— a child becomes visible it should not. Switch-agnostic: a DocShare grant widens read on any
	path, which is exactly the over-grant the can_read_row oracle exists to catch."""
	def plant():
		user = roster.email(f"grain_{grain_idx + 1}")
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


def _partner_callog_cross_tenant():
	"""DATA (A13 partner abuse, CALL surface): mint a CRM Call Log on an out-of-grain (grain_3) lead
	and DocShare it WRITE to the partner. native_would_allow on that concrete Call Log must then report
	write allowed — the cross-tenant attribution/write the partner-call grain scope must never permit
	(mirrors _partner_write_out_of_grain on the lead surface, on the call-log row). The generator seeds
	Leads+Tasks but NOT Call Logs, so the row is created here inside the caller-owned savepoint."""
	def plant():
		user = roster.email("partner")
		leaked_lead = _lead_in_grain(2)  # grain_3 — outside the partner's grain_1
		doc = frappe.new_doc("CRM Call Log")
		# CRM Call Log autoname is field:id — set a synthetic id, exactly as partner_call._upsert_one does.
		doc.id = f"AUTHZ-A13-{frappe.generate_hash(length=8)}"
		doc.set("custom_external_id", f"AUTHZ-A13-MUT-{frappe.generate_hash(length=6)}")
		doc.type = "Incoming"
		doc.status = "Completed"
		setattr(doc, "from", "")
		doc.to = ""
		doc.reference_doctype = "CRM Lead"
		doc.reference_docname = leaked_lead
		doc.insert(ignore_permissions=True)
		_grant_docshare("CRM Call Log", doc.name, user, read=1, write=1)
		return {"user": user, "doc": frappe.get_doc("CRM Call Log", doc.name)}

	def detect(ctx):
		return native_would_allow(ctx["user"], "CRM Call Log", "write", ctx["doc"])

	return plant, detect


def _guest_routing_coercion():
	"""DATA/BEHAVIOURAL (A14 public-intake guest abuse): the intake fold hands the lead-create brain
	(partner._upsert_one) is_sysmgr=False + mp, so allow_routing is False and a Guest's submitted
	routing is DROPPED in favour of the form's grain. The bug we guard is "if allow_routing ever
	flips, the foreign routing sticks". We can't edit code from a test, so we SIMULATE the flip's
	effect: drive the brain in the routing-ALLOWED mode (is_sysmgr=True, mp=None) with a foreign-routing
	payload — the EXACT lead a guest-routing-accepted bug would mint — and confirm the foreign vertical
	stuck. detect() True = the foreign routing is observable, proving the routing-coercion check (the
	A14 case asserts vertical == the form grain) is NOT blind (mirrors A2's program-only-match sim)."""
	from tatva_connect.api import partner

	def plant():
		g_form = grains.GRAINS[0]     # the form/expected grain (grain_1)
		g_foreign = grains.GRAINS[2]  # grain_3 — the smuggled foreign grain
		doc, _action = partner._upsert_one(
			{
				"mobile_no": "9990014001",
				"custom_vertical": g_foreign["vertical"],
				"custom_group": g_foreign["group"],
				"custom_current_program": g_foreign["program"],
			},
			None, True, ["mobile_no"], {}, allowed_programs=[],
		)
		return {"lead": doc.name, "expected_vertical": g_form["vertical"],
		        "foreign_vertical": g_foreign["vertical"]}

	def detect(ctx):
		v = frappe.db.get_value("CRM Lead", ctx["lead"], "custom_vertical")
		# Detected iff the foreign routing stuck (it did, because we removed the clamp) and is NOT the
		# form's grain — exactly the leak the is_sysmgr=False+mp path prevents on the real fold.
		return v == ctx["foreign_vertical"] and v != ctx["expected_vertical"]

	return plant, detect


def _smartview_grain_overgrant():
	"""DATA (A12 grain-clamp / A7 column leak, SMART VIEW surface): over-grant a grain entitlement by
	adding a probe to an OUT-OF-GRAIN Assignment Rule. The Smart View catalog resolves through
	entitlement.resolve_fields keyed on entitled_grains(), so once the foreign grain is entitled a field
	scoped to it enters the catalog — the out-of-grain column the grain clamp (_grains_from_axes /
	_validate_columns) exists to stop. Mirrors _grant_permlevel1_read: prove a clean baseline (foreign
	grain NOT entitled, foreign column absent), grant, then prove the delta — no dependency on a seeded
	catalog row (the foreign-scoped field row is synthetic, present iff its grain is entitled)."""
	from tatva_connect.access import entitlement
	from tatva_connect.tests.authz.generator import TAG as _TAG

	g_out = grains.GRAINS[2]  # grain_3 — the foreign line to over-grant
	# A synthetic field TICKED BY grain_3's internal contract: since Phase 5/9 grain membership is the contract's tick list, not a grain_* column on the row, so the tick is what scopes it.
	foreign_key = "lead:authz_mut_grain3_col"
	foreign_row = {"field_key": foreign_key, "section": "lead", "fieldname": "authz_mut_grain3_col"}
	role = "Authz Mut SmartView Probe Role"
	probe = "authz.mut.svprobe@example.test"
	out_rule = "{}::{}".format(_TAG, g_out["key"])  # the grain_3 Assignment Rule seeded by generator

	def plant():
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}
			               ).insert(ignore_permissions=True)
		if not frappe.db.exists("User", probe):
			frappe.get_doc({
				"doctype": "User", "email": probe, "first_name": "svprobe",
				"user_type": "System User", "send_welcome_email": 0, "roles": [{"role": role}],
			}).insert(ignore_permissions=True)
		# The tick is a Link, so the catalog row must exist before the contract can reference it.
		if not frappe.db.exists("CRM Lead API Field", foreign_key):
			frappe.get_doc({
				"doctype": "CRM Lead API Field", "field_key": foreign_key, "section": "lead",
				"label": "Authz Mut Grain3 Col", "fieldname": "authz_mut_grain3_col",
			}).insert(ignore_permissions=True)
		# Tick the synthetic field into grain_3's internal contract — that tick IS its grain scoping.
		contract = frappe.db.get_value("CRM Lead API Mapping", {
			"is_internal": 1, "vertical": g_out["vertical"], "crm_group": g_out["group"],
			"program": g_out["program"],
		})
		if contract:
			doc = frappe.get_doc("CRM Lead API Mapping", contract)
			if foreign_key not in [r.field for r in doc.allowed_fields]:
				doc.append("allowed_fields", {"field": foreign_key})
				doc.save(ignore_permissions=True)
		# Clean baseline: the probe is entitled to NO grain (no Assignment Rule membership), so the
		# grain_3-scoped column is absent from its resolved catalog.
		_clear_access_caches()
		base = entitlement.resolve_fields(
			{foreign_key: foreign_row}, entitlement.entitled_grains(probe), [role])
		baseline_has = foreign_key in base
		# The over-grant: enrol the probe into the grain_3 Assignment Rule -> entitled_grains now
		# includes grain_3, so the foreign-scoped column resolves into the catalog.
		rule = frappe.get_doc("Assignment Rule", out_rule)
		rule.append("users", {"user": probe})
		rule.save(ignore_permissions=True)
		_clear_access_caches()
		return {"probe": probe, "key": foreign_key, "row": foreign_row, "role": role,
		        "baseline_has": baseline_has}

	def detect(ctx):
		_clear_access_caches()
		resolved = entitlement.resolve_fields(
			{ctx["key"]: ctx["row"]}, entitlement.entitled_grains(ctx["probe"]), [ctx["role"]])
		# Detected iff the foreign-grain column that was ABSENT at baseline is now in the catalog.
		return (ctx["key"] in resolved) and not ctx["baseline_has"]

	return plant, detect


def _synthetic_endpoint_responses(action):
	"""The two synthetic HTTP responses the B-vector detector feeds the escalation judgment: the
	KNOWN-BAD (the endpoint DID the thing) and the KNOWN-GOOD control (the endpoint DENIED). Keyed by
	the action shape, mirroring _endpoint_allowed: a read/list/info leaks by returning a non-empty
	`message`; a write/create/delete leaks by a clean 2xx."""
	if action in ("read", "list", "info"):
		bad = (200, {"message": ["authz-mut-leaked-row"]})  # non-empty payload = data was returned
	else:
		bad = (200, {"message": "ok"})                       # clean 2xx = write/create/delete succeeded
	good = (403, {})                                         # endpoint refused = no escalation
	return good, bad


def _endpoint_escalation(action, doctype, seed_file=False):
	"""CODE mutation (B1..B5, the endpoint layer): the sweep runs over REAL HTTP (committed lifecycle),
	which an in-process rollback test can't fire, so we mutate the sweep's ESCALATION DETECTOR instead.
	We feed test_endpoint_sweep._escalates — the ONE judgment the live sweep runs — a KNOWN-BAD synthetic
	response (the endpoint returned/did the thing) on a (doctype, action) the REAL oracle DENIES for a
	hostile principal, and confirm it is flagged. Only the HTTP response is synthetic; the oracle side is
	genuine (native_http_verdict on a seeded foreign-owned row). A KNOWN-GOOD control (endpoint denied)
	must NOT flag, proving the detector discriminates rather than firing blindly."""
	from tatva_connect.tests.authz.test_endpoint_sweep import _escalates

	def plant():
		user = roster.email("no_role")  # the strict catastrophe floor: zero roles, natively denied
		name = None
		if seed_file:
			# a PRIVATE File owned by Administrator — the B5 IDOR target, minted in the caller's savepoint.
			f = frappe.get_doc({
				"doctype": "File", "file_name": f"authz-mut-{frappe.generate_hash(length=6)}.txt",
				"is_private": 1, "content": "authz-mut-secret",
			}).insert(ignore_permissions=True)
			name = f.name
		elif doctype and action not in ("list", "info"):
			name = _lead_in_grain(0)  # a seeded foreign-owned CRM Lead
		native_ok = native_http_verdict(user, action, doctype, name)
		return {"action": action, "native_ok": native_ok}

	def detect(ctx):
		# The oracle side must be GENUINE: a principal the real engine denies. If it somehow allows, the
		# plant is vacuous (no known-bad to flag) -> report as not-detected, never a false pass.
		if ctx["native_ok"]:
			return False
		good, bad = _synthetic_endpoint_responses(ctx["action"])
		flagged_bad = _escalates(ctx["action"], bad[0], bad[1], ctx["native_ok"])
		flagged_good = _escalates(ctx["action"], good[0], good[1], ctx["native_ok"])
		# Detected iff the detector FLAGS the known-bad escalation AND does NOT flag the known-good control.
		return flagged_bad and not flagged_good

	return plant, detect


# ---- the registry of planted violations ---------------------------------------------------------
# AT LEAST ONE per attack vector. Untestable-without-code-mutation vectors are declared explicitly
# (never silently skipped) so the self-validation can surface them as build warnings.

def _build_mutations():
	a1p, a1d = _share_out_of_grain(3, 0)           # grain_4 user, share a grain_1 lead
	a2p, a2d = _share_out_of_grain(3, 4)           # grain_4 user, share a grain_5 (shared-program) lead
	a3p, a3d = _open_doctype_to_role("CRM Lead", "Purchase Master Manager", "write")  # cross-app role gains CRM Lead write capability
	a4p, a4d = _remove_field_restriction_effect("Sales User")
	a5p, a5d = _share_out_of_grain(0, 3)           # roll-up flavour: a non-report grain leaks in
	a6p, a6d = _share_doc_write("Sales User", 0)   # DocShare write → the would_allow ceiling a bypass path must not exceed
	a7p, a7d = _grant_permlevel1_read()
	a8p, a8d = _share_child_parent(3, 0)
	a10p, a10d = _share_doc_write("WhatsApp User", 0)  # wrapped-method data ceiling = native_would_allow on a shared doc
	a11p, a11d = _open_doctype_to_role("Contact", "Purchase Master Manager")
	a12p, a12d = _share_out_of_grain(4, 2)         # DocShare over-grant (the share IS the fence abuse)
	a12svp, a12svd = _smartview_grain_overgrant()  # entitlement over-grant -> Smart View catalog leaks a foreign column
	a13p, a13d = _partner_write_out_of_grain()
	a13clp, a13cld = _partner_callog_cross_tenant()  # cross-tenant write on the CALL surface
	a14p, a14d = _guest_routing_coercion()
	# B1..B5 — the endpoint layer: mutate the sweep's escalation detector with a known-bad HTTP response
	# on a (doctype, action) the oracle genuinely denies for no_role (the hostile floor).
	b1p, b1d = _endpoint_escalation("read", "CRM Lead")    # IDOR object read
	b2p, b2d = _endpoint_escalation("write", "CRM Lead")   # IDOR object write
	b3p, b3d = _endpoint_escalation("list", "CRM Lead")    # BFLA: unauthorised list
	b4p, b4d = _endpoint_escalation("info", None)          # excessive info disclosure (no doctype oracle)
	b5p, b5d = _endpoint_escalation("read", "File", seed_file=True)  # private-file IDOR

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
		 "english": "a permlevel-0-only probe role granted permlevel-1 read on CRM Lead -> the grain "
		            "fields enter its permitted-field set (proves the field-read-leak detector to a "
		            "principal WITHOUT permlevel-1 read is not blind, audit C1)",
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

		{"attack": "A12", "id": "MUT-A12-smartview-grain-overgrant",
		 "english": "a probe is enrolled into an out-of-grain Assignment Rule -> entitlement widens and "
		            "the Smart View catalog (entitlement.resolve_fields) surfaces a foreign-grain column "
		            "the grain clamp must stop",
		 "plant": a12svp, "detect": a12svd, "expected_detector": "entitlement.resolve_fields",
		 "untestable_without_code_mutation": None},

		{"attack": "A13", "id": "MUT-A13-partner-cross-tenant-write",
		 "english": "the partner (mapped to grain_1) is DocShared write on a TatvaPractice lead -> "
		            "cross-tenant write grant (attribution abuse, invariant 16)",
		 "plant": a13p, "detect": a13d, "expected_detector": "oracle.native_would_allow",
		 "untestable_without_code_mutation": None},

		{"attack": "A13", "id": "MUT-A13-partner-callog-cross-tenant",
		 "english": "the partner (mapped to grain_1) is DocShared write on a grain_3 lead's CRM Call Log "
		            "-> cross-tenant write on the CALL surface (would_allow on the concrete row)",
		 "plant": a13clp, "detect": a13cld, "expected_detector": "oracle.native_would_allow",
		 "untestable_without_code_mutation": None},

		{"attack": "A14", "id": "MUT-A14-guest-routing-coercion",
		 "english": "the lead-create brain driven in routing-ALLOWED mode with a foreign-routing payload "
		            "mints a lead on the foreign grain — the EXACT leak the intake fold's is_sysmgr=False+mp "
		            "(allow_routing=False) prevents; proves the routing-coercion check is not blind",
		 "plant": a14p, "detect": a14d, "expected_detector": "intake routing coercion (lead.custom_vertical)",
		 "untestable_without_code_mutation": None},

		{"attack": "A14", "id": "MUT-A14-guest-master-growth",
		 "english": "a Guest manual-field submit must never grow a master (intake._ensure_master Guest "
		            "guard); the guard is a pure code branch, so the differential lives in the CASE",
		 "plant": None, "detect": None, "expected_detector": "row count of the growable master (CASE)",
		 "untestable_without_code_mutation":
			 "The Guest master-growth guard is a pure CODE branch (intake.py:355 "
			 "`if frappe.session.user == 'Guest': return canonical`). Its failure mode is deleting that "
			 "branch — a code mutation, which a test can't perform on app source. There is no DATA "
			 "condition that flips frappe.session.user for an in-process oracle. The protection is the "
			 "CASE A14-guest-no-master-growth, which IS a differential: it grows nothing as Guest and "
			 "+1 as an authed caller against the SAME growable master (CRM Side Effect Option) — so if "
			 "line 355 were deleted, the Guest no-grow assertion would fail. Recorded here, not silently "
			 "skipped (audit M2)."},

		{"attack": "B1", "id": "MUT-B1-endpoint-idor-read",
		 "english": "the endpoint-sweep escalation detector is fed a 200-with-payload read of a foreign "
		            "CRM Lead that native denies no_role -> must be flagged (IDOR object read), while a "
		            "denied response is not",
		 "plant": b1p, "detect": b1d, "expected_detector": "test_endpoint_sweep._escalates",
		 "untestable_without_code_mutation": None},

		{"attack": "B2", "id": "MUT-B2-endpoint-idor-write",
		 "english": "a clean-200 write on a foreign CRM Lead that native denies no_role must be flagged "
		            "(IDOR object write), while a 403 is not",
		 "plant": b2p, "detect": b2d, "expected_detector": "test_endpoint_sweep._escalates",
		 "untestable_without_code_mutation": None},

		{"attack": "B3", "id": "MUT-B3-endpoint-bfla-list",
		 "english": "a 200-with-rows list of CRM Lead that native denies no_role must be flagged "
		            "(unauthorised function / BFLA), while an empty/denied response is not",
		 "plant": b3p, "detect": b3d, "expected_detector": "test_endpoint_sweep._escalates",
		 "untestable_without_code_mutation": None},

		{"attack": "B4", "id": "MUT-B4-endpoint-info-disclosure",
		 "english": "a 200-with-payload info response (no doctype -> oracle deny-expected) must be flagged "
		            "(excessive data disclosure), while a denied response is not",
		 "plant": b4p, "detect": b4d, "expected_detector": "test_endpoint_sweep._escalates",
		 "untestable_without_code_mutation": None},

		{"attack": "B5", "id": "MUT-B5-endpoint-private-file-idor",
		 "english": "a 200-with-payload read of a private File that native denies no_role must be flagged "
		            "(BOLA on File), while a denied response is not",
		 "plant": b5p, "detect": b5d, "expected_detector": "test_endpoint_sweep._escalates",
		 "untestable_without_code_mutation": None},
	]


MUTATIONS = _build_mutations()


def testable():
	"""Mutations that plant a detectable bug (have plant+detect)."""
	return [m for m in MUTATIONS if m["plant"] is not None and m["detect"] is not None]


def untestable():
	"""Mutations declared untestable-without-code-mutation (surfaced as build warnings)."""
	return [m for m in MUTATIONS if m["untestable_without_code_mutation"]]
