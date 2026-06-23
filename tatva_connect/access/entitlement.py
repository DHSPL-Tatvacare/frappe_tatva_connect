"""Grain field entitlement — the ONE brain for "which fields may an internal user see".

The catalog (`CRM Lead API Field`) is shared with the partner API, but this module is read
ONLY on the internal Smart Views path. It never touches partner behaviour:
  * `entitled_grains(user)` — the grains a principal owns (partner → mapping row; internal →
    their Assignment Rule rows + reports_to roll-up; System Manager → all; nothing → universal).
  * `field_in_grains(field_row, grains)` — a containment test (blank axis = wildcard, no throw).
  * `resolve_fields(...)` — catalog ∩ grain − role-restricted ∪ universal (fail-closed).

A grain is a `(vertical, group, program)` tuple; a blank axis means "any". `ALL_GRAINS` is the
System-Manager sentinel that matches every field without enumerating the masters.
"""
import frappe

# System Manager sees every field — a sentinel so we never enumerate the grain masters.
ALL_GRAINS = "__all__"

# Always present regardless of grain or role restriction — the minimum that lets a user
# identify a lead (fail-closed floor; a restriction can never hide these).
UNIVERSAL_KEYS = ("lead:name", "lead:mobile_no", "lead:status")

_GRAINS_CACHE = "tatva_connect:entitled_grains"
_RESTRICT_CACHE = "tatva_connect:field_restrictions"
_REPORTS_TO_DEPTH = 10


def _request_cache(bucket, key, builder):
	"""Per-request memoisation on frappe.local (cleared each request). One read per user."""
	store = getattr(frappe.local, bucket, None)
	if store is None:
		store = {}
		setattr(frappe.local, bucket, store)
	if key not in store:
		store[key] = builder()
	return store[key]


def _grain(row):
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
	"""Users whose reports_to is `user` (one rung of the management chain)."""
	return frappe.get_all("User", filters={"reports_to": user}, pluck="name")


def _internal_grains(user):
	"""Union of the grains on every CRM Lead Assignment Rule the user (or anyone reporting up to
	them) is a member of. reports_to is walked breadth-first with a visited-set + depth cap so a
	circular or deep chain can never loop or blow the stack."""
	grains, seen, frontier, depth = set(), {user}, [user], 0
	while frontier and depth <= _REPORTS_TO_DEPTH:
		rule_names = frappe.get_all(
			"Assignment Rule User",
			filters={"user": ["in", frontier], "parenttype": "Assignment Rule"},
			pluck="parent",
		)
		for r in frappe.get_all(
			"Assignment Rule",
			filters={"name": ["in", rule_names], "document_type": "CRM Lead"},
			fields=["grain_vertical", "grain_group", "grain_program"],
		):
			grains.add(_grain(r))
		nxt = []
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

	return _request_cache(_GRAINS_CACHE, user, build)


def field_in_grains(field_row, grains):
	"""Membership: is this catalog field visible to a principal holding `grains`?
	True iff ∃ g ∈ grains where every axis of the field is blank (wildcard) or equals g's axis.
	Containment only — never throws (unlike resolve_scoped, which raises on ties)."""
	if grains == ALL_GRAINS:
		return True
	fv, fg, fp = _grain(field_row)
	for gv, gg, gp in grains:
		if (not fv or fv == gv) and (not fg or fg == gg) and (not fp or fp == gp):
			return True
	return False


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
		hidden |= _request_cache(_RESTRICT_CACHE, role, lambda role=role: set(frappe.get_all(
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
		if field_in_grains(row, grains):
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
