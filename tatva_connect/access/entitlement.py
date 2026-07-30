"""Grain field entitlement — the ONE brain for "which fields may an internal user see".

The catalog (`CRM Lead API Field`) is shared with the partner API, but this module is read
ONLY on the internal Smart Views path. It never touches partner behaviour:
  * `entitled_grains(user)` — the grains a principal owns (partner → mapping row; internal → their
    Assignment Rule rows + reports_to roll-up, or native User Permission once `Access::Grain::registry`
    is armed; System Manager → all; nothing → universal).
  * `field_in_grains_via_contract(field_key, grains)` — is this field ticked by any grain's contract?
  * `resolve_fields(...)` — catalog ∩ grain − role-restricted ∪ universal (fail-closed).

A grain is a `(vertical, group, program)` tuple; a blank axis means "any". `ALL_GRAINS` is the
System-Manager sentinel that matches every field without enumerating the masters.
"""
import frappe

from tatva_connect import automation
from tatva_connect.access import request_cache
from tatva_connect.taxonomy import grain as taxonomy_grain

# System Manager sees every field — a sentinel so we never enumerate the grain masters.
ALL_GRAINS = "__all__"

# Dormant. ON -> entitlement is read from native User Permission and clamped by the CRM Grain registry.
REGISTRY_FLAG = "Access::Grain::registry"

_GRAINS_CACHE = "tatva_connect:entitled_grains"
_RESTRICT_CACHE = "tatva_connect:field_restrictions"
_INTERNAL_TICKS_CACHE = "tatva_connect:internal_contract_ticks"
_UNIVERSAL_CACHE = "tatva_connect:internal_universal_fields"
_REGISTRY_FLAG_CACHE = "tatva_connect:grain_registry_flag"
_REGISTRY_ROWS_CACHE = "tatva_connect:grain_registry_rows"
_REPORTS_TO_DEPTH = 10

# The grain axes, in tuple order, as the master doctype a User Permission is granted on.
_UP_AXES = ("CRM Vertical", "CRM Group", "CRM Program")


def _grain(row):
	"""Grain tuple off an Assignment Rule row (grain_vertical/group/program). Assignment Rule keeps these
	columns — the catalog's grain_* were dropped in Phase 9. Read ONLY for the rule roll-up below."""
	return (row.get("grain_vertical") or "", row.get("grain_group") or "", row.get("grain_program") or "")


def _partner_grain(user):
	"""The grain off the caller's enabled CRM Lead API Mapping row, or None if not a partner."""
	mp = frappe.db.get_value(
		"CRM Lead API Mapping", {"partner_user": user, "enabled": 1},
		["vertical", "crm_group", "program"], as_dict=True,
	)
	if not mp:
		return None
	return {(mp.vertical or "", mp.crm_group or "", mp.program or "")}


def _direct_reports(user):
	"""Users whose reports_to is `user` (one rung of the management chain). `reports_to` is an
	HRMS/Employee field, NOT present on User in a stock CRM — so this is guarded by the caller
	(`_can_rollup`) and is a no-op until a reporting field exists. Full hierarchy roll-up via
	CRM Sales Hierarchy is deferred (Q4); a manager meanwhile gets their own rule grains."""
	return frappe.get_all("User", filters={"reports_to": user}, pluck="name")


def _internal_grains(user):
	"""Union of the grains on every CRM Lead Assignment Rule the user (or anyone reporting up to
	them) is a member of. reports_to is walked breadth-first with a visited-set + depth cap so a
	circular or deep chain can never loop or blow the stack. The roll-up only runs when User
	actually has a `reports_to` field (else it cleanly no-ops to the user's own rule grains)."""
	can_rollup = frappe.db.has_column("User", "reports_to")
	grains, seen, frontier, depth = set(), {user}, [user], 0
	while frontier and depth <= _REPORTS_TO_DEPTH:
		rule_names = frappe.get_all(
			"Assignment Rule User",
			filters={"user": ["in", frontier], "parenttype": "Assignment Rule"},
			pluck="parent",
		)
		for r in frappe.get_all(
			"Assignment Rule",
			# disabled:0 — a disabled rule grants NO lead access, so it contributes no grains
			# (else entitlement diverges from the assignment-driven lead visibility).
			filters={"name": ["in", rule_names], "document_type": "CRM Lead", "disabled": 0},
			fields=["grain_vertical", "grain_group", "grain_program"],
		):
			g = _grain(r)
			# A fully-blank grain (every axis blank) is NOT a wildcard here — it means the
			# rule carries no scope, so it grants no entitlement (fail-closed; a partial
			# grain like (vertical, '', '') still legitimately wildcards its blank axes).
			if any(g):
				grains.add(g)
		nxt = []
		if can_rollup:
			for u in frontier:
				for report in _direct_reports(u):
					if report not in seen:
						seen.add(report)
						nxt.append(report)
		frontier, depth = nxt, depth + 1
	return grains


def _registry_enabled():
	"""Is the registry source armed? Request-cached because `settings.is_enabled` reads the DB fresh on
	every call and `grain_entitled` is asked once PER ROW on the bulk-import path."""
	return request_cache(_REGISTRY_FLAG_CACHE, "flag", lambda: automation.is_enabled(REGISTRY_FLAG))


def _registry_grains():
	"""Every declared `(vertical, group, program)` in the CRM Grain registry. Request-cached."""
	def build():
		return {
			(r.vertical or "", r.group or "", r.program or "")
			for r in frappe.get_all("CRM Grain", fields=["vertical", "`group` as `group`", "program"])
		}
	return request_cache(_REGISTRY_ROWS_CACHE, "all", build)


def _grains_from_user_permission(user):
	"""The user's entitled REGION, read from native User Permission — the flag-ON source.

	Frappe AND-s User Permissions ACROSS doctypes: a lead must sit in the allowed vertical AND the allowed
	group AND the allowed program. So the region is the cross product of the three axes, and an axis
	carrying NO permission is left BLANK — a wildcard, exactly as `taxonomy.grain` reads it. That blank is
	what lets a rep entitled to all of Goodflip-Care/Anaya work every programme under it, including one
	whose first lead does not exist yet.

	Read through frappe's own `get_allowed_docs_for_doctype`, never a raw User Permission query: our grain
	permissions are narrow (`applicable_for`), so each is stored TWICE — once for CRM Lead and once for
	CRM Deal. The native helper resolves `applicable_for` / `apply_to_all_doctypes` and returns only what
	applies to CRM Lead; a raw query returns both rows and double-counts every axis.

	No permission on ANY axis -> the empty set, never a blank `("", "", "")` tuple: that tuple would be a
	three-way wildcard granting everything. A user with nothing configured is entitled to nothing — the
	same fail-closed rule the Assignment-Rule source spells as `if any(g)`.
	"""
	from frappe.permissions import get_allowed_docs_for_doctype, get_user_permissions

	permissions = get_user_permissions(user)
	axes = [
		sorted(get_allowed_docs_for_doctype(permissions.get(doctype, []), "CRM Lead"))
		for doctype in _UP_AXES
	]
	if not any(axes):
		return set()
	verticals, groups, programs = (axis or [""] for axis in axes)
	return {(v, g, p) for v in verticals for g in groups for p in programs}


def entitled_grains(user=None):
	"""The grains a principal may see fields for. Request-cached.
	  * System Manager        -> ALL_GRAINS (every field).
	  * partner (mapping row) -> that row's grain.
	  * internal              -> native User Permission when `Access::Grain::registry` is armed,
	                             else their Assignment Rule grains + reports_to roll-up.
	  * none of the above     -> empty set (universal fields only; fail-closed)."""
	user = user or frappe.session.user

	def build():
		if "System Manager" in frappe.get_roles(user):
			return ALL_GRAINS
		partner = _partner_grain(user)
		if partner is not None:
			return partner
		# The swap. Flag OFF keeps the Assignment-Rule roll-up below byte-identical; the dead roll-up is
		# removed only once the flag has proved out, so a disarm is a true revert and not a rebuild.
		if _registry_enabled():
			return _grains_from_user_permission(user)
		return _internal_grains(user)

	return request_cache(_GRAINS_CACHE, user, build)


def _internal_ticks():
	"""{grain_tuple: set(ticked field_keys)} from the is_internal=1 mappings — the SAME tick mechanism
	the partner API reads, but for internal per-grain visibility. Request-cached; one build per request.
	Seeded by access/internal_contract.py from the frozen GRAIN_FIELDS snapshot (the primary seed)."""
	def build():
		ticks = {}
		for m in frappe.get_all(
			"CRM Lead API Mapping", filters={"is_internal": 1},
			fields=["name", "vertical", "crm_group", "program"],
		):
			g = (m.vertical or "", m.crm_group or "", m.program or "")
			ticks[g] = set(frappe.get_all(
				"CRM Lead API Mapping Field", filters={"parent": m.name}, pluck="field",
			))
		return ticks
	return request_cache(_INTERNAL_TICKS_CACHE, "all", build)


def field_in_grains_via_contract(field_key, grains):
	"""The ONE membership brain: True iff `field_key` is ticked by a contract COVERING any grain in
	`grains`. ALL_GRAINS (System Manager) → True. Reads NO grain_* column (they were dropped in Phase 9) —
	the per-grain contract, seeded from GRAIN_FIELDS, is the sole source of internal field visibility.

	The covers question is `taxonomy.grain.covers` and ONLY it: this module once spelled the wildcard rule
	locally with `==`, which compares bytes where MariaDB compares case-insensitively — the same divergence
	class that hid 129 fields from 1,894 leads. One matcher, one home, casefold included."""
	if grains == ALL_GRAINS:
		return True
	for contract_grain, keys in _internal_ticks().items():
		if field_key not in keys:
			continue
		candidate = dict(zip(taxonomy_grain.AXES, contract_grain, strict=True))
		if any(taxonomy_grain.covers(candidate, *g) for g in grains):
			return True
	return False


def entitled_to_field(field_key, grains):
	"""Could ANY of the caller's ENTITLED grains reach this field?

	An entitlement is a RULE: a user granted a whole vertical carries a blank group meaning ANY. So this
	is the `overlaps` question, and `field_in_grains_via_contract` beside it is the `covers` one — that
	answers about a real LEAD, whose blank axis is a literal blank. Asking the lead question about an
	entitlement collapsed a vertical-wide admin to the universal floor while the rep beneath them saw
	their group's full set. `taxonomy/grain.py` names this trap in its own docstring."""
	if grains == ALL_GRAINS:
		return True
	return any(field_in_any_grain_overlapping(field_key, g) for g in grains)


def _rule_axes(rule_grain):
	"""A rule grain as three normalised axes. Blank stays blank — it MEANS ANY and must never be
	back-filled with a value, which is what makes it safe to hand to `taxonomy.grain.overlaps`."""
	v, g, p = (rule_grain or ("", "", ""))
	return (v or ""), (g or ""), (p or "")


def field_in_any_grain_overlapping(field_key, rule_grain):
	"""Could a RULE declaring `rule_grain` EVER be allowed this field? The wildcard-aware sibling of
	`field_in_grains_via_contract`.

	The one above asks about a real lead, so its grain is DATA and blank is the literal empty string. This
	one is asked by author-time surfaces — a workflow's declared grain, where a blank axis means ANY — and
	answering it with the data-grain matcher hides every field a MORE SPECIFIC contract ticks: a workflow
	scoped to a whole vertical was offered only the fields of contracts equally blank, though execution
	would have allowed far more. Same catalog, same ticks, same `taxonomy.grain` module — a different,
	honestly-named question, exactly as `is_set_declared` sits beside `is_settable`.
	"""
	rv, rg, rp = _rule_axes(rule_grain)
	for contract_grain, keys in _internal_ticks().items():
		if field_key not in keys:
			continue
		candidate = dict(zip(taxonomy_grain.AXES, contract_grain, strict=True))
		if taxonomy_grain.overlaps(candidate, rv, rg, rp):
			return True
	return False


def grain_overlaps_entitlement(rule_grain, user=None):
	"""Could a RULE at `rule_grain` ever concern a record this user may act on?

	The possibility question, for author-time surfaces. `grain_entitled` is the actuality question and
	takes a real record's DATA grain; this takes a rule grain whose blank axis means ANY on the ASKING
	side too, so it is symmetric and resolves through `taxonomy.grain.overlaps`. Handing a rule grain to
	`grain_entitled` would compare that wildcard as the empty string and answer confidently wrong.
	"""
	grains = entitled_grains_within(rule_grain, user)
	return grains == ALL_GRAINS or bool(grains)


def entitled_grains_within(rule_grain, user=None):
	"""WHICH of the caller's entitled grains fall inside `rule_grain` — the FIELD scope for reading a
	surface that declares one.

	The set-valued form of the question above, and the one a reader must ask. Entitlement decides which
	fields a user may see; a surface's own declared grain may only NARROW that, never widen it. Resolving
	a view's fields against the view's grain instead handed a rep columns their entitlement withholds.

	ALL_GRAINS (System Manager) stays ALL_GRAINS. A rule declaring no axis narrows nothing — it is not a
	rule about no records, it is a rule about any of them. Both sides are RULE grains, so the question is
	`taxonomy.grain.overlaps` and never `covers`; nothing here re-spells that comparison."""
	grains = entitled_grains(user)
	if grains == ALL_GRAINS:
		return grains
	rv, rg, rp = _rule_axes(rule_grain)
	if not (rv or rg or rp):
		return grains
	return {
		(gv, gg, gp)
		for gv, gg, gp in grains
		if taxonomy_grain.overlaps({"vertical": gv, "group": gg, "program": gp}, rv, rg, rp)
	}


def users_entitled_to(rule_grain, txt=None, limit=20, scan=500):
	"""The users a rule at this grain may legitimately name — the picker's answer.

	Deliberately a FILTER over candidates rather than a reverse query over Assignment Rule rows: the
	forward question ("is this user entitled here") already has exactly one answer, and a reverse query
	would be a second matcher free to disagree with it — which is the defect class this brain exists to
	remove. `scan` bounds the sweep so a picker can never walk an unbounded user table, and `txt` narrows
	it the way an ordinary Link search does.
	"""
	filters = {"enabled": 1, "user_type": "System User"}
	if txt:
		filters["name"] = ["like", f"%{txt}%"]
	found = []
	for user in frappe.get_all("User", filters=filters, pluck="name", limit=scan, order_by="name asc"):
		if grain_overlaps_entitlement(rule_grain, user=user):
			found.append(user)
			if len(found) >= limit:
				break
	return found


def is_universal_field(field_key):
	"""Contract-era 'universal': True iff `field_key` is ticked by EVERY internal contract (belongs to all
	grains). Request-cached off the same _internal_ticks() map. No contracts at all → False (fail-closed)."""
	def build():
		# Only contracts that DECLARE something: a zero-tick grain means "declares nothing yet", and intersecting it would flip every universal field off everywhere.
		declared = [keys for keys in _internal_ticks().values() if keys]
		if not declared:
			return set()
		return set.intersection(*declared)
	return field_key in request_cache(_UNIVERSAL_CACHE, "all", build)


def grain_entitled(grain, user=None):
	"""Is an explicit `(vertical, group, program)` grain within the caller's entitlement?
	True iff ∃ entitled g that COVERS the requested grain — every axis of g is blank (wildcard)
	or equals the requested axis (so entitlement to a whole vertical covers a group within it).
	The clamp that stops a client widening scope past entitled_grains() on the internal path."""
	grains = entitled_grains(user)
	if grains == ALL_GRAINS:
		# System Manager keeps the bypass DELIBERATELY: the registry check below would otherwise lock an
		# admin out of the very off-registry records they exist to remediate.
		return True
	rv, rg, rp = grain
	# Flag ON, and ALONGSIDE the covers check below, never instead of it: a region can cover a tuple that
	# is not a declared business slice (a typo'd group sits inside its vertical just as neatly as a real
	# one), and being covered is not the same as existing. Structural enforcement, not a second matcher.
	if _registry_enabled() and (rv, rg, rp) not in _registry_grains():
		return False
	for gv, gg, gp in grains:
		# The ONE wildcard-match predicate. This loop used to spell the rule out a second time, and a
		# second copy of "blank means ANY" is exactly the defect that hid 129 fields from 1,894 leads.
		if taxonomy_grain.covers({"vertical": gv, "group": gg, "program": gp}, rv, rg, rp):
			return True
	return False


def groups_under(vertical):
	"""The groups declared under `vertical` in the CRM Grain registry, sorted.

	INERT this phase — nothing calls it yet, exactly as nothing read CRM Grain in Phase 0. It exists so
	the WRITE side (a create form resolving a wildcard axis to one concrete child) and the FILTER side
	have one declared source to ask, rather than each growing its own `SELECT DISTINCT` over the masters.
	"""
	return sorted({
		row.group
		for row in frappe.get_all("CRM Grain", filters={"vertical": vertical}, fields=["`group` as `group`"])
		if row.group
	})


def programs_under(vertical, group):
	"""The concrete programmes declared under `(vertical, group)` in the CRM Grain registry, sorted.

	A blank-programme row is a REGION, not a pickable child — a lead exists before programme enrolment —
	so it is deliberately not returned: offering "" in a picker would ask a user to choose the absence of
	a choice. INERT this phase, same as `groups_under`.
	"""
	return sorted({
		row.program
		for row in frappe.get_all(
			"CRM Grain", filters={"vertical": vertical, "group": group}, fields=["program"]
		)
		if row.program
	})


def _restricted_keys(roles):
	"""field_keys hidden from ANY of these roles (CRM Lead Field Restriction). Request-cached
	per role. Read ONLY here — the partner API never consults this doctype."""
	hidden = set()
	for role in roles:
		hidden |= request_cache(_RESTRICT_CACHE, role, lambda role=role: set(frappe.get_all(
			"CRM Lead Field Restriction", filters={"role": role}, pluck="field",
		)))
	return hidden


def restrict_fields(catalog_rows, roles):
	"""The ROLE half alone: the same rows, minus any field hidden from these roles.

	For a catalog whose grain is already settled. An activity type's key IS its grain — the type is
	reached through the view's grain and a caller not entitled to it never gets this far — so its fields
	need no second admission, only this. `resolve_fields` below is this plus the grain question, and is
	the right call whenever the grain is still open.

	A restriction hides a field outright: there is no exempt list. A floor written here would be a second
	answer to "may this role see it", competing with the restriction seed that already decides."""
	hidden = _restricted_keys(roles)
	return {k: r for k, r in catalog_rows.items() if k not in hidden}


def resolve_fields(catalog_rows, grains, roles):
	"""The internal field list: catalog rows visible in `grains`, minus any field restricted for `roles`.

	"Universal" is what the contracts TICK (`is_universal_field`) — never a list held in code. A hardcoded
	floor is a second brain: it drifts (one of its three keys named a field that does not exist), and it
	disagreed with the seeds by 49 fields.

	No entitlement means NO fields, not a courtesy floor. A caller with nothing configured has nothing to
	show, and the surface says so plainly instead of handing them two fields and a dead end."""
	rows = restrict_fields(catalog_rows, roles)
	if grains == ALL_GRAINS:
		return rows
	if not grains:
		return {}
	return {
		key: row
		for key, row in rows.items()
		if is_universal_field(row["field_key"]) or entitled_to_field(row["field_key"], grains)
	}


@frappe.whitelist()
def my_grain_pick_options():
	"""The create form's wildcard pickers: for each entitled region, the declared children of the axis
	it leaves blank. `{region_key: {axis, fieldname, label, values}}`, region_key = `vertical::group::program`.

	A create form has to land a lead on ONE leaf, so a region that wildcards an axis must ask. This is the
	WRITE side, and it reads the REGISTRY — what the operator has declared — never the lead table, which is
	the FILTER side's source. Merging the two would offer a programme only after somebody had already
	managed to create a lead in it, so the first Sigrima lead could never be made.

	Returns `{}` while `Access::Grain::registry` is dormant, so the form renders exactly as it does today
	and the flag remains the single switch for this behaviour — the frontend needs no flag of its own.
	Read-only: it enumerates config the caller may already read, and grants nothing.
	"""
	if not _registry_enabled():
		return {}
	grains = entitled_grains()
	if grains == ALL_GRAINS or not grains:
		return {}
	out = {}
	for vertical, group, program in grains:
		if not vertical:
			continue  # a wildcard vertical has no declared-children helper; the stamp leaves it alone too
		key = f"{vertical}::{group or ''}::{program or ''}"
		if not group:
			values = groups_under(vertical)
			axis, fieldname, label = "group", "custom_group", frappe._("Group")
		elif not program:
			values = programs_under(vertical, group)
			axis, fieldname, label = "program", "custom_current_program", frappe._("Program")
		else:
			continue  # fully concrete — nothing to ask
		if values:
			out[key] = {"axis": axis, "fieldname": fieldname, "label": label, "values": values}
	return out


@frappe.whitelist()
def my_entitled_grains():
	"""The caller's entitled grains, for the Smart View editor's grain selector. Each grain is a
	{vertical, group, program} dict; `all=True` means System Manager (every grain)."""
	grains = entitled_grains()
	if grains == ALL_GRAINS:
		return {"all": True, "grains": []}
	return {
		"all": False,
		"grains": [{"vertical": v, "group": g, "program": p} for v, g, p in sorted(grains)],
	}
