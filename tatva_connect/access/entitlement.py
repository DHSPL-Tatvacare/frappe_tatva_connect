"""Grain field entitlement — the ONE brain for "which fields may an internal user see".

The catalog (`CRM Lead API Field`) is shared with the partner API, but this module is read
ONLY on the internal Smart Views path. It never touches partner behaviour:
  * `entitled_grains(user)` — the grains a principal owns (partner → mapping row; internal →
    their Assignment Rule rows + reports_to roll-up; System Manager → all; nothing → universal).
  * `field_in_grains_via_contract(field_key, grains)` — is this field ticked by any grain's contract?
  * `resolve_fields(...)` — catalog ∩ grain − role-restricted ∪ universal (fail-closed).

A grain is a `(vertical, group, program)` tuple; a blank axis means "any". `ALL_GRAINS` is the
System-Manager sentinel that matches every field without enumerating the masters.
"""
import frappe

from tatva_connect.access import request_cache

# System Manager sees every field — a sentinel so we never enumerate the grain masters.
ALL_GRAINS = "__all__"

# Always present regardless of grain or role restriction — the minimum that lets a user
# identify a lead (fail-closed floor; a restriction can never hide these).
UNIVERSAL_KEYS = ("lead:name", "lead:mobile_no", "lead:status")

_GRAINS_CACHE = "tatva_connect:entitled_grains"
_RESTRICT_CACHE = "tatva_connect:field_restrictions"
_INTERNAL_TICKS_CACHE = "tatva_connect:internal_contract_ticks"
_UNIVERSAL_CACHE = "tatva_connect:internal_universal_fields"
_REPORTS_TO_DEPTH = 10


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


def entitled_grains(user=None):
	"""The grains a principal may see fields for. Request-cached.
	  * System Manager        -> ALL_GRAINS (every field).
	  * partner (mapping row) -> that row's grain.
	  * internal              -> their Assignment Rule grains + reports_to roll-up.
	  * none of the above     -> empty set (universal fields only; fail-closed)."""
	user = user or frappe.session.user

	def build():
		if "System Manager" in frappe.get_roles(user):
			return ALL_GRAINS
		partner = _partner_grain(user)
		if partner is not None:
			return partner
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


def _contract_covers(contract_grain, grain):
	"""THE grain-match rule, identical to taxonomy.grain._score and every other matcher: a SET axis on the
	CONTRACT must equal the target's; a BLANK axis is a wildcard. Contracts are declared at the level
	visibility is granted (a rep sees all of GoodFlip Care/Anaya whatever program the patient enrolled
	into), so a blank program covers every program — it never means the empty string."""
	# strict=True: a grain that is not a full 3-tuple is a defect (every input surface populates all three axes), and a short one would zip to nothing and match EVERYTHING.
	return all(c == "" or c == t for c, t in zip(contract_grain, grain, strict=True))


def field_in_grains_via_contract(field_key, grains):
	"""The ONE membership brain: True iff `field_key` is ticked by a contract COVERING any grain in
	`grains`. ALL_GRAINS (System Manager) → True. Reads NO grain_* column (they were dropped in Phase 9) —
	the per-grain contract, seeded from GRAIN_FIELDS, is the sole source of internal field visibility."""
	if grains == ALL_GRAINS:
		return True
	for contract_grain, keys in _internal_ticks().items():
		if field_key in keys and any(_contract_covers(contract_grain, g) for g in grains):
			return True
	return False


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
		return True
	rv, rg, rp = grain
	for gv, gg, gp in grains:
		if (not gv or gv == rv) and (not gg or gg == rg) and (not gp or gp == rp):
			return True
	return False


def _restricted_keys(roles):
	"""field_keys hidden from ANY of these roles (CRM Lead Field Restriction). Request-cached
	per role. Read ONLY here — the partner API never consults this doctype."""
	hidden = set()
	for role in roles:
		hidden |= request_cache(_RESTRICT_CACHE, role, lambda role=role: set(frappe.get_all(
			"CRM Lead Field Restriction", filters={"role": role}, pluck="field",
		)))
	return hidden


def resolve_fields(catalog_rows, grains, roles):
	"""The internal field list: catalog rows visible in `grains`, minus any field restricted for
	`roles`, plus the universal keys (always present). `catalog_rows` is the already scope-filtered
	catalog ({field_key: row}) so this stays the single grain+restriction brain with no second
	catalog read. Returns the surviving {field_key: row} dict, order preserved."""
	hidden = _restricted_keys(roles)
	out = {}
	for key, row in catalog_rows.items():
		if key in UNIVERSAL_KEYS:
			out[key] = row  # universal floor — never grain/restriction filtered
			continue
		if key in hidden:
			continue
		if field_in_grains_via_contract(row["field_key"], grains):
			out[key] = row
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
