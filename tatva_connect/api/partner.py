"""Gated partner lead API — the ONLY surface external partners touch.

Partners are role-less System Users. Raw `/api/resource/*` returns 403 for them
(crm's `org_hierarchy.has_lead_permission` blocks non-managers). These methods are
their entire contract — every one resolves the caller's `CRM Lead API Mapping` row:

  * has an enabled mapping row  -> EXTERNAL partner. Routing (source / vertical /
      group / program) is FORCED from the row; reads/writes are scoped to that
      line; the fields they may send/read are THEIR ticked subset of the catalog.
  * System Manager, no mapping  -> TRUSTED internal (e.g. MyTatvaCore). Sends
      routing in the body; full catalog; unscoped.
  * neither                      -> 403.

ONE set of endpoints serves every partner — what varies per partner is config on
their mapping row (routing + allowed-fields grid), never code.

Singular:
  GET    lead_schema  -> the fields THIS caller may send/read (+ their routing)
  GET    lead_get     -> one lead by `name` or `mobile_no`, scoped to the line
  POST   lead_create  -> create-or-upsert by phone; returns the CRM `name`
  PUT    lead_update  -> update a lead by CRM `name` (the id POST returned)
  DELETE lead_delete  -> delete a lead by CRM `name`, scoped to the line
Bulk / query (each record enforced individually; partial success):
  POST   lead_create_bulk  -> {"leads":[...]}  (<= 100)
  PUT    lead_update_bulk   -> {"updates":[{"name":..,..}]}  (<= 100)
  DELETE lead_delete_bulk   -> {"names":[...]}  (<= 100)
  POST   lead_get_bulk      -> {"names":[...]} or {"mobile_nos":[...]} (<= 100)
  GET    lead_list          -> curated filters + pagination, line-scoped
"""
import frappe
from frappe import _
from frappe.utils import cint, cstr

from tatva_connect import automation
from tatva_connect.api._base import (
	_PARTNER_PATH,
	_api,
	_cfg,
	_classify,
	_fail,
	_meter_volume,
	_norm_phone,
	_ok,
	_read_list,
	_resolve_caller,
	_run_bulk,
	normalise_partner_response,
)

# ---------------------------------------------------------------------------
# The catalog (the platform superset a partner CAN be granted) is DATA, not code:
# it lives in the `CRM Lead API Field` master, one row per namespaced `section:fieldname`
# key, carrying its routing (section / target doctype / child-table fieldname).
# `_catalog()` reads + caches that table; everything below derives from it, so
# exposing a new partner field = one `CRM Lead API Field` row, no code change.
#
# The only structural code-side constant: the parent (non-child) section is "lead".
# A row whose `child_table_field` is set is a child section; everything else is
# treated as a parent (CRM Lead) field.
# ---------------------------------------------------------------------------
_CATALOG_CACHE_KEY = "tatva_connect:lead_api_catalog"
# Backstop TTL: the Partner::Catalog::cache toggle evicts instantly on edit when ON; this bounds
# staleness to 60 min even if that hook never fires (toggle off, or a missed event) — self-heals.
_CATALOG_CACHE_TTL_SEC = 60 * 60
PARENT_SECTION = "lead"


def _build_catalog():
	"""Read the `CRM Lead API Field` table into the structured catalog the API uses.
	Returns a dict (everything below is derived from these keys):
	  keys           ordered list of `section:fieldname` (sort_field=field_key)
	  key_set        set(keys)
	  section_doctype  {section: target doctype}
	  section_child    {section: child-table fieldname}  (child sections only)
	  section_title    {section: display title}
	  section_key_field {section: fieldname}  (multi-row child sections only — the
	                     row that carries is_row_key; its presence = multi-row, A4)
	"""
	rows = frappe.get_all(
		"CRM Lead API Field",
		fields=["field_key", "section_key", "target_doctype", "child_table_field", "fieldname", "is_row_key", "sql_source"],
		order_by="field_key asc",
	)
	keys, section_doctype, section_child, section_title, section_key_field = [], {}, {}, {}, {}
	for r in rows:
		# Smart-Views-only activity rows (CRM Task promoted columns / payload) are NOT lead
		# fields — the partner API exposes lead fields only. Skip them so they never reach
		# a partner's lead_schema. Parent/child (CRM Lead) rows stay; blank sql_source =
		# legacy partner-only rows stay.
		if r.sql_source in ("task", "payload"):
			continue
		# Grain routing fields (source / vertical / group / program) are FORCED from entitlement,
		# never partner-suppliable (see ROUTING_FIELDS). They ARE Smart-View catalog rows (so the
		# composer can filter/column by Product Line / Group / Program) but must never enter a
		# partner's writable lead_schema — skip them here. The Smart View path reads them directly.
		if r.fieldname in ROUTING_FIELDS:
			continue
		keys.append(r.field_key)
		section = r.section_key
		section_doctype[section] = r.target_doctype
		if r.child_table_field:
			section_child[section] = r.child_table_field
		if r.is_row_key:
			section_key_field[section] = r.fieldname
		section_title.setdefault(section, section.title())
	return {
		"keys": keys,
		"key_set": set(keys),
		"section_doctype": section_doctype,
		"section_child": section_child,
		"section_title": section_title,
		"section_key_field": section_key_field,
	}


def _catalog():
	"""The cached catalog. Invalidated by clear_catalog_cache on CRM Lead API Field write."""
	cached = frappe.cache().get_value(_CATALOG_CACHE_KEY)
	if cached is None:
		cached = _build_catalog()
		frappe.cache().set_value(_CATALOG_CACHE_KEY, cached, expires_in_sec=_CATALOG_CACHE_TTL_SEC)
	return cached


def clear_catalog_cache(doc=None, method=None):
	"""doc_events hook (CRM Lead API Field on_update/on_trash) — drop the cached catalog."""
	if not automation.is_enabled("Partner::Catalog::cache"):
		return
	frappe.cache().delete_value(_CATALOG_CACHE_KEY)

# Forced from entitlement for partners, accepted from a trusted System Manager. Never a
# partner-WRITABLE field — _build_catalog skips these so they can be Smart-View catalog rows
# (read/filter/column on Product Line / Group / Program) without entering a partner's lead_schema.
ROUTING_FIELDS = ("source", "custom_vertical", "custom_group", "custom_current_program")

# All numeric caps (bulk size, list page sizes) live on the CRM Partner API Settings
# Single, read fresh via _cfg() — there is no module-local copy (one source of truth).

# Per child-table write/read contract (§3 of partner-api-child-table-contract.md).
#   multi_row -> each row is addressed by key_field: upsert-by-key, never clobber.
#   single-row -> one merged row; a 2nd distinct incoming row -> 400.
# The key_field is intrinsic to the row and unique within one lead; it is marked
# `required` in the schema and is ALWAYS preserved on write (even if a partner's
# grid doesn't tick it), mirroring the always-included lead:mobile_no pattern.
# DERIVED FROM THE CATALOG (A4): a child section is multi-row iff one of its catalog
# rows carries `is_row_key`; that row's fieldname is the key. No hardcoded map.

# lead_list: only these (safe, indexed) filters are honoured. NOT arbitrary fields.
#   key in request -> (CRM Lead field, operator)
LIST_FILTERS = {
	"status": ("status", "="),
	"created_after": ("creation", ">="),
	"created_before": ("creation", "<="),
	"updated_after": ("modified", ">="),
	"updated_before": ("modified", "<="),
}


# TATVA: removed unused `catalog_label(key)` — dead code with no caller anywhere (audit #32, A.14).


def catalog_section_title(child_fieldname):
	"""Readable section title for a child-table fieldname, e.g. 'custom_lab_profile'
	-> 'Lab' — used in child-write error messages ('report_date is required to
	identify a Lab row'). Falls back to the fieldname if the section is unknown."""
	cat = _catalog()
	for section, cf in cat["section_child"].items():
		if cf == child_fieldname:
			return cat["section_title"].get(section, section)
	return child_fieldname


def _child_key_field(cf):
	"""The row-key fieldname for a child-table fieldname, or None (single-row).
	Derived from the catalog's `is_row_key` flag (A4) — replaces CHILD_CONFIG."""
	cat = _catalog()
	for section, child in cat["section_child"].items():
		if child == cf:
			return cat["section_key_field"].get(section)
	return None


# -- helpers -----------------------------------------------------------------
# Entity-agnostic plumbing (_resolve_caller, _norm_phone, _ok/_fail, _classify,
# _api, _cfg, _run_bulk, _read_list, normalise_partner_response) now lives in
# tatva_connect.api._base and is imported above. Only lead-specific helpers remain
# in this module.

def _allowed_keys(user, has_mapping):
	"""The catalog keys THIS caller may use. Partner with a non-empty grid -> that
	subset (mobile_no always included). Empty grid, or System Manager -> full catalog."""
	cat = _catalog()
	if has_mapping:
		picked = frappe.get_all(
			"CRM Lead API Mapping Field",
			filters={"parent": user, "parenttype": "CRM Lead API Mapping"},
			pluck="field",
		)
		picked = {k for k in picked if k in cat["key_set"]}
		if picked:
			picked.add("lead:mobile_no")
			return [k for k in cat["keys"] if k in picked]
	return list(cat["keys"])


def _allowed_programs(user, has_mapping):
	"""The programs a multi-program key may set per lead. Read from the mapping's
	`allowed_programs` grid. Empty for forced-program keys (Niva) and trusted internal —
	in those paths it is never consulted, so their behaviour is unchanged."""
	if not has_mapping:
		return []
	return frappe.get_all(
		"CRM Lead API Mapping Program",
		filters={"parent": user, "parenttype": "CRM Lead API Mapping"},
		pluck="program",
	)


def _resolve_program(item, mp, allowed_programs):
	"""Resolve the lead's program from the key's MODE. The forced/list/none logic lives
	in the shared resolver (tatva_connect.taxonomy.program_mode) — the SAME brain the
	enrolment form uses — so both intake paths behave identically. Only the trusted
	(no-mapping, internal caller) case is partner-specific and stays here."""
	if not mp:
		return item.get("custom_current_program")  # trusted internal caller, unchanged
	from tatva_connect.taxonomy.program_mode import resolve_program

	return resolve_program(
		mp.program, allowed_programs, item.get("custom_current_program"),
		field_label="custom_current_program", source_label="key",
	)


def _split_keys(keys):
	"""namespaced keys -> (parent_fieldnames, {child_fieldname: [fieldnames]})."""
	cat = _catalog()
	section_child = cat["section_child"]
	parent_fields = []
	child_allow = {cf: [] for cf in section_child.values()}
	for k in keys:
		section, _, fieldname = k.partition(":")
		if section in section_child:
			child_allow[section_child[section]].append(fieldname)
		else:
			parent_fields.append(fieldname)
	return parent_fields, child_allow


def _caller_fields():
	"""(user, mp, is_sysmgr, parent_fields, child_allow) for the resolved caller."""
	user, mp, is_sysmgr = _resolve_caller()
	parent_fields, child_allow = _split_keys(_allowed_keys(user, bool(mp)))
	return user, mp, is_sysmgr, parent_fields, child_allow


def _collect(data, parent_fields, child_allow, allow_routing):
	"""Pull ONLY this caller's allowed parent fields + child arrays from a payload.
	Routing is included only for a trusted caller (allow_routing)."""
	parent = {}
	for fn in parent_fields:
		val = data.get(fn)
		if val not in (None, ""):
			parent[fn] = val
	if allow_routing:
		for fn in ROUTING_FIELDS:
			if data.get(fn):
				parent[fn] = data.get(fn)
	if parent.get("mobile_no"):
		parent["mobile_no"] = _norm_phone(parent["mobile_no"])

	children = {}
	for cf, allowed in child_allow.items():
		rows = data.get(cf)
		if not rows or not allowed:
			continue
		if isinstance(rows, str):
			rows = frappe.parse_json(rows)
		if not isinstance(rows, list):
			rows = [rows]
		# The key_field of a multi-row child is the row's address: always keep it
		# (even if the partner's grid didn't tick it) plus the explicit _delete flag,
		# so the upsert engine can identify the row. Everything else is allow-gated.
		keep = set(allowed)
		key_field = _child_key_field(cf)
		if key_field:
			keep.add(key_field)
		keep.add("_delete")
		children[cf] = [{k: v for k, v in (r or {}).items() if k in keep} for r in rows]
	return parent, children


def _resolve_picklists(parent, children, grain):
	"""Translate human picklist VALUES -> grain-scoped composite PKs on the collected parent + child
	dicts BEFORE they reach the doc. This is the ONE correct place: Frappe runs _validate_links()
	before any before_validate hook on insert/save, so a doc_event can't fix a Link — the ingestion
	path must resolve first. Rides the SAME taxonomy.picklist brain the lead_schema discovery
	advertises, so what a partner is TOLD they may send is exactly what is ACCEPTED. Shared by
	create + update."""
	from tatva_connect.taxonomy import picklist

	cm = frappe.get_meta("CRM Lead")
	parent = picklist.resolve_row_links("CRM Lead", parent, grain)
	children = {
		cf: [picklist.resolve_row_links(cm.get_field(cf).options, r, grain) for r in rows]
		for cf, rows in children.items()
	}
	return parent, children


def _child_error(message, field):
	"""Raise a ValidationError that carries the offending field so the unified error
	contract can emit `error.fields` (frappe.throw alone only carries a message)."""
	e = frappe.ValidationError(message)
	e.fields = [field]
	frappe.throw(message, e)


def _merge_row(target, incoming):
	"""Overlay only the sent fields onto an existing child row (partial update)."""
	for k, v in (incoming or {}).items():
		if k == "_delete":
			continue
		target.set(k, v)


def _apply_single_row(doc, cf, incoming, title):
	"""single-row child: merge sent fields onto the one row (create if none).
	A 2nd distinct incoming row is ambiguous -> 400."""
	if len(incoming) > 1:
		_child_error(_("{0} accepts a single row").format(title), cf)
	rows = doc.get(cf) or []
	if rows:
		_merge_row(rows[0], incoming[0])
	else:
		doc.append(cf, {k: v for k, v in (incoming[0] or {}).items() if k != "_delete"})


def _apply_multi_row(doc, cf, incoming, key_field, title):
	"""multi-row child, upsert-by-key. Each incoming row MUST carry key_field (else 400).
	Match by key -> partial-merge that row; new key -> append; {key,_delete:true} ->
	drop that keyed row. Rows already on the doc that are not referenced -> untouched."""
	rows = doc.get(cf) or []
	# Index existing rows by the STRINGIFIED key: a stored Date is a date object but
	# the incoming key arrives as an ISO string from JSON — cstr keys both uniformly
	# (same normalisation _latest_lab_row uses) so match-by-key actually matches.
	by_key = {}
	for r in rows:
		by_key.setdefault(cstr(r.get(key_field)), r)
	for row in incoming:
		raw = (row or {}).get(key_field)
		if raw in (None, ""):
			_child_error(
				_("{0} is required to identify a {1} row").format(key_field, title), key_field
			)
		key = cstr(raw)
		target = by_key.get(key)
		if row.get("_delete"):
			if target is not None:
				doc.remove(target)
				by_key.pop(key, None)
			continue
		if target is not None:
			_merge_row(target, row)
		else:
			new = doc.append(cf, {k: v for k, v in row.items() if k != "_delete"})
			by_key[key] = new


def _apply_children(doc, children):
	"""Config-driven UPSERT-BY-KEY write engine (§3-4 of the child-table contract).
	Per child table, the catalog's is_row_key decides single-row vs multi-row + the key field;
	a partial write never wipes the other fields/rows already on the doc.
	After applying, mirror the latest lab row's headline metrics up to the parent
	(the validate hook also does this, but doing it here keeps the API path explicit
	and idempotent)."""
	for cf, incoming in children.items():
		if not incoming:
			continue
		key_field = _child_key_field(cf)
		title = catalog_section_title(cf)
		if key_field:
			_apply_multi_row(doc, cf, incoming, key_field, title)
		else:
			_apply_single_row(doc, cf, incoming, title)
	from tatva_connect.lead.leads import sync_headline_metrics
	sync_headline_metrics(doc)


def _force_routing(doc, mp):
	"""Stamp the partner's fixed routing — they can never set or change it."""
	if mp.source:
		doc.source = mp.source
	if mp.vertical:
		doc.custom_vertical = mp.vertical
	if mp.crm_group:
		doc.custom_group = mp.crm_group
	if mp.program:
		doc.custom_current_program = mp.program


# -- response contract -------------------------------------------------------
# The shared _ok / _fail writers (the top-level {status:...} envelope) live in
# _base and are imported above. The lead-shaped result/curate shims stay here.

def _result(doc, action):
	_ok(action=action, data={
		"name": doc.name, "source": doc.source, "vertical": doc.custom_vertical,
		"group": doc.custom_group, "program": doc.custom_current_program,
	})


def _curate(doc, parent_fields, child_allow):
	"""A lead as only the caller's allowed fields (+ name + read-only routing)."""
	out = {fn: doc.get(fn) for fn in parent_fields}
	out.update({
		"name": doc.name, "source": doc.source, "custom_vertical": doc.custom_vertical,
		"custom_group": doc.custom_group, "custom_current_program": doc.custom_current_program,
	})
	for cf, allowed in child_allow.items():
		if allowed:
			# Return the caller's allowed fields + our row id (name) + the key field
			# (the row's address per the contract — always present even if not ticked).
			key_field = _child_key_field(cf)
			keys = list(allowed)
			if key_field and key_field not in keys:
				keys.append(key_field)
			out[cf] = [dict({k: r.get(k) for k in keys}, name=r.get("name")) for r in (doc.get(cf) or [])]
	return out


# -- the lead's API contract — ONE source of truth for what the API actually enforces, so
# lead_schema's `required` flags match behaviour (and can't drift from it).
LEAD_IDENTITY = "mobile_no"   # the dedup anchor -> required
_NAMELESS = "(no name)"       # placeholder for a lead sent without a name (the doctype requires one)
# Parent fields the caller may OMIT (reported not-required, overriding the doctype's reqd flag):
# first_name -> we fill the placeholder; status -> crm's CRM Lead controller defaults it.
LEAD_OPTIONAL = ("first_name", "status")
_LEAD_REQUIRED = {LEAD_IDENTITY: True, **{fn: False for fn in LEAD_OPTIONAL}}


# -- per-record core (shared by singular + bulk) -----------------------------

def _upsert_one(item, mp, is_sysmgr, parent_fields, child_allow, allowed_programs=None):
	"""Create-or-upsert one lead from a dict. Returns (doc, action)."""
	mobile = _norm_phone(item.get(LEAD_IDENTITY))
	if not mobile:
		frappe.throw(_("{0} is required").format(LEAD_IDENTITY))
	parent, children = _collect(item, parent_fields, child_allow, allow_routing=bool(is_sysmgr and not mp))
	parent[LEAD_IDENTITY] = mobile

	program = _resolve_program(item, mp, allowed_programs)
	# "List mode" key (mapping program blank, e.g. Anaya): the caller supplies
	# custom_current_program per lead. Program is a MUTABLE ATTRIBUTE, never identity ->
	# the dedup lane is phone + product line + group for EVERY key. Re-sending a patient
	# with a new program transitions the SAME lead; it never creates a second one.
	open_program = bool(mp and not mp.program)

	anchor_vertical = mp.vertical if mp else item.get("custom_vertical")
	anchor_group = mp.crm_group if mp else item.get("custom_group")
	# Resolve human picklist VALUES -> grain composite PKs now the grain is known, before the doc
	# is built (Frappe validates Links before any hook). Covers the partner API AND the intake fold.
	parent, children = _resolve_picklists(
		parent, children, (anchor_vertical or "", anchor_group or "", program or "")
	)
	existing = frappe.db.get_value(
		"CRM Lead",
		{"mobile_no": mobile, "custom_vertical": anchor_vertical, "custom_group": anchor_group},
		"name",
	)
	if existing:
		doc = frappe.get_doc("CRM Lead", existing)
		doc.update(parent)
		_apply_children(doc, children)
		if mp:
			_force_routing(doc, mp)
		if open_program and program:
			doc.custom_current_program = program
		doc.save(ignore_permissions=True)
		return doc, "updated"

	parent.setdefault("first_name", _NAMELESS)  # status is left for crm's controller to default
	doc = frappe.new_doc("CRM Lead")
	doc.update(parent)
	_apply_children(doc, children)
	if mp:
		_force_routing(doc, mp)
	if open_program:
		doc.custom_current_program = program
	doc.insert(ignore_permissions=True)
	return doc, "created"


def _update_one(name, item, mp, is_sysmgr, parent_fields, child_allow):
	"""Update one lead by CRM name. Returns (doc, 'updated'). Scope-checked for partners."""
	if not name:
		frappe.throw(_("name (the CRM Lead id) is required for an update"))
	# Missing AND out-of-scope return the SAME generic not-found (no ID, no "scope"
	# hint) so a partner can't probe which lead ids exist on another line.
	if not frappe.db.exists("CRM Lead", name):
		frappe.throw(_("Lead not found"), frappe.DoesNotExistError)
	doc = frappe.get_doc("CRM Lead", name)
	if mp and (doc.custom_vertical != mp.vertical or doc.custom_group != mp.crm_group):
		frappe.throw(_("Lead not found"), frappe.DoesNotExistError)
	parent, children = _collect(item, parent_fields, child_allow, allow_routing=bool(is_sysmgr and not mp))
	grain = (
		(mp.vertical if mp else doc.custom_vertical) or "",
		(mp.crm_group if mp else doc.custom_group) or "",
		(doc.custom_current_program or ""),
	)
	parent, children = _resolve_picklists(parent, children, grain)
	doc.update(parent)
	_apply_children(doc, children)
	if mp:
		_force_routing(doc, mp)
	doc.save(ignore_permissions=True)
	return doc, "updated"


def _delete_one(name, mp):
	"""Delete one lead by CRM name. Scope-checked for partners."""
	if not name:
		frappe.throw(_("name (the CRM Lead id) is required to delete"))
	# Missing AND out-of-scope return the SAME generic not-found (no ID, no "scope"
	# hint) so a partner can't probe which lead ids exist on another line.
	if not frappe.db.exists("CRM Lead", name):
		frappe.throw(_("Lead not found"), frappe.DoesNotExistError)
	doc = frappe.get_doc("CRM Lead", name)
	if mp and (doc.custom_vertical != mp.vertical or doc.custom_group != mp.crm_group):
		frappe.throw(_("Lead not found"), frappe.DoesNotExistError)
	frappe.delete_doc("CRM Lead", name, ignore_permissions=True)


# -- singular endpoints ------------------------------------------------------

@frappe.whitelist(methods=["GET"])
@_api
def lead_schema(**kwargs):
	"""Discovery: the fields THIS caller may send/read + their routing mode.
	Two partners hitting this get different field lists — driven by their grid."""
	user, mp, _is_sysmgr, parent_fields, child_allow = _caller_fields()

	def describe(doctype, fields, required_override=None):
		"""Field dicts for a section. `required_override` ({fieldname: bool}) reports the API's
		actual contract instead of the doctype's `reqd` flag — used for the parent (identity
		required, defaulted fields not) and a child key_field (required to address its row)."""
		required_override = required_override or {}
		m = frappe.get_meta(doctype)
		out = []
		for fn in fields:
			f = m.get_field(fn)
			if not f:
				continue
			entry = {
				"fieldname": fn, "label": f.label, "type": f.fieldtype,
				"required": required_override.get(fn, bool(f.reqd)),
				"options": (f.options or None) if f.fieldtype in ("Link", "Select") else None,
			}
			# Controlled vocabulary for any grain-scoped composite-PK Link (picklist, stage, ...):
			# advertise the exact human values this caller may send — the SAME registry the ingestion
			# resolver (taxonomy.picklist.resolve_row_links) dispatches through, so discovery ==
			# ingestion, always. Only when the grain is fixed (a partner mapping); a trusted caller
			# supplies its own grain, so there's no single value list.
			if mp and f.fieldtype == "Link":
				from tatva_connect.taxonomy import picklist

				vals = picklist.values_for(f.options, (mp.vertical, mp.crm_group, mp.program or ""), fn)
				if vals:
					entry["allowed_values"] = vals
			out.append(entry)
		return out

	cat = _catalog()
	children = {}
	for section, cf in cat["section_child"].items():
		allowed = child_allow.get(cf)
		if not allowed:
			continue
		key_field = cat["section_key_field"].get(section)
		# The key_field addresses a multi-row child: surface it in the schema even if
		# the partner's grid didn't tick it, marked required (it IS, for the write).
		fields = list(allowed)
		if key_field and key_field not in fields:
			fields.append(key_field)
		children[cf] = {
			"multi_row": bool(key_field),
			"key_field": key_field,
			"fields": describe(cat["section_doctype"][section], fields,
			                   required_override={key_field: True} if key_field else None),
		}

	out = {
		"lead": describe("CRM Lead", parent_fields, required_override=_LEAD_REQUIRED),
		"children": children,
		"child_write": "Send each child as a JSON array under its key, e.g. "
		               "custom_lab_profile=[{\"report_date\":\"2026-01-15\", ...}]. Multi-row "
		               "children are upsert-by-key on key_field: a new key adds a row, the same "
		               "key updates that row (only sent fields change), rows you omit are untouched. "
		               "Single-row children merge onto the one row. Delete a row with "
		               "{<key_field>, \"_delete\": true}.",
		"dedup": "A lead is unique per (mobile_no, product line, group). Re-sending the same "
		         "patient updates that lead. Program is NOT part of identity: sending the same "
		         "patient with a different program transitions the SAME lead, never a new one.",
		"bulk": {"max_per_call": _cfg()["bulk_max_records"], "list_page_max": _cfg()["list_max_page"],
		         "list_filters": [*list(LIST_FILTERS.keys()), "mobile_no"]},
	}
	if mp and not mp.program:
		# Open-program key: line + group forced; program mode is LIST if the key has an
		# allowed_programs set, else NONE. Both derived from config, no hardcoding.
		ap = _allowed_programs(user, True)
		out["routing"] = {
			"mode": "list" if ap else "none",
			"source": mp.source, "vertical": mp.vertical, "group": mp.crm_group,
			"program": None,
			"allowed_programs": ap,
			"program_required": bool(ap),
			"note": (
				"Line and group are fixed. Send custom_current_program from allowed_programs on "
				"every lead. Program is a mutable attribute, NOT identity: the same patient on a "
				"new program is the SAME lead (a transition)."
			) if ap else "Line and group are fixed. This key uses no program.",
		}
	elif mp:
		out["routing"] = {
			"mode": "forced", "source": mp.source, "vertical": mp.vertical,
			"group": mp.crm_group, "program": mp.program,
			"note": "Your routing is fixed. Any routing fields you send are ignored.",
		}
	else:
		out["routing"] = {
			"mode": "caller-supplied", "fields": list(ROUTING_FIELDS),
			"note": "Trusted caller: send these routing fields in the body.",
		}
	_ok(data=out)


@frappe.whitelist(methods=["GET"])
@_api
def lead_get(**kwargs):
	"""Read one lead by `name` or `mobile_no`. A partner only sees leads on their
	line, and only their allowed fields."""
	_user, mp, _is_sysmgr, parent_fields, child_allow = _caller_fields()
	data = frappe.form_dict
	filters = {}
	if data.get("name"):
		filters["name"] = data.get("name")
	elif data.get("mobile_no"):
		filters["mobile_no"] = _norm_phone(data.get("mobile_no"))
	else:
		frappe.throw(_("name or mobile_no is required"))
	if mp:
		filters["custom_vertical"] = mp.vertical
		filters["custom_group"] = mp.crm_group

	lead_name = frappe.db.get_value("CRM Lead", filters, "name")
	if not lead_name:
		frappe.throw(_("Lead not found"), frappe.DoesNotExistError)
	_ok(action="fetched", data=_curate(frappe.get_doc("CRM Lead", lead_name), parent_fields, child_allow))


@frappe.whitelist(methods=["POST"])
@_api
def lead_create(**kwargs):
	"""Create-or-upsert a lead by phone. Returns the CRM `name` to PUT back to."""
	user, mp, is_sysmgr, parent_fields, child_allow = _caller_fields()
	allowed_programs = _allowed_programs(user, bool(mp))
	doc, action = _upsert_one(frappe.form_dict, mp, is_sysmgr, parent_fields, child_allow, allowed_programs)
	return _result(doc, action)


@frappe.whitelist(methods=["PUT"])
@_api
def lead_update(**kwargs):
	"""Update a lead by CRM `name`. Partner scope-checked; can't move it to another line."""
	_user, mp, is_sysmgr, parent_fields, child_allow = _caller_fields()
	doc, action = _update_one(frappe.form_dict.get("name"), frappe.form_dict, mp, is_sysmgr, parent_fields, child_allow)
	return _result(doc, action)


@frappe.whitelist(methods=["DELETE"])
@_api
def lead_delete(**kwargs):
	"""Delete a lead by CRM `name`. Partner scope-checked (own line only). A lead with
	linked activity raises LinkExistsError — so a partner can't nuke a worked lead."""
	_user, mp, _is_sysmgr, _parent_fields, _child_allow = _caller_fields()
	name = frappe.form_dict.get("name")
	_delete_one(name, mp)
	_ok(action="deleted", data={"name": name})


# -- bulk / query endpoints --------------------------------------------------

@frappe.whitelist(methods=["POST"])
@_api(bulk=True)
def lead_create_bulk(**kwargs):
	"""Create-or-upsert many leads. Body: {"leads":[{...}, ...]} (<= 100). Partial success."""
	user, mp, is_sysmgr, parent_fields, child_allow = _caller_fields()
	allowed_programs = _allowed_programs(user, bool(mp))
	leads = _read_list(frappe.form_dict, "leads") or []

	def one(i, item):
		doc, action = _upsert_one(item, mp, is_sysmgr, parent_fields, child_allow, allowed_programs)
		return {"index": i, "status": "success", "action": action, "name": doc.name}

	return _run_bulk(leads, one)


@frappe.whitelist(methods=["PUT"])
@_api(bulk=True)
def lead_update_bulk(**kwargs):
	"""Update many leads. Body: {"updates":[{"name":..,..fields}, ...]} (<= 100). Partial success."""
	_user, mp, is_sysmgr, parent_fields, child_allow = _caller_fields()
	updates = _read_list(frappe.form_dict, "updates") or []

	def one(i, item):
		doc, action = _update_one((item or {}).get("name"), item, mp, is_sysmgr, parent_fields, child_allow)
		return {"index": i, "status": "success", "action": action, "name": doc.name}

	return _run_bulk(updates, one)


@frappe.whitelist(methods=["DELETE"])
@_api(bulk=True)
def lead_delete_bulk(**kwargs):
	"""Delete many leads. Body: {"names":[...]} (<= 100). Partial success."""
	_user, mp, _is_sysmgr, _parent_fields, _child_allow = _caller_fields()
	names = _read_list(frappe.form_dict, "names") or []

	def one(i, name):
		_delete_one(name, mp)
		return {"index": i, "status": "success", "action": "deleted", "name": name}

	return _run_bulk(names, one)


@frappe.whitelist(methods=["POST"])
@_api(bulk=True)
def lead_get_bulk(**kwargs):
	"""Read many leads by `names` OR `mobile_nos` (<= 100). Out-of-scope ids omitted."""
	_user, mp, _is_sysmgr, parent_fields, child_allow = _caller_fields()
	data = frappe.form_dict
	names = _read_list(data, "names")
	mobiles = _read_list(data, "mobile_nos")
	if not names and not mobiles:
		frappe.throw(_("names or mobile_nos is required"))

	# input-ordered: results[i] corresponds to the i-th requested id, found or not.
	by = "name" if names else "mobile_no"
	requested = list(names) if names else [_norm_phone(m) for m in mobiles]
	bulk_max = _cfg()["bulk_max_records"]
	if len(requested) > bulk_max:
		frappe.throw(_("Max {0} per call; received {1}. Page the rest.").format(bulk_max, len(requested)))
	denied = _meter_volume(len(requested), "read")  # read volume = rows requested
	if denied is not None:
		return denied

	filters = {by: ["in", requested]}
	if mp:
		filters["custom_vertical"] = mp.vertical
		filters["custom_group"] = mp.crm_group
	by_id = {}
	for r in frappe.get_all("CRM Lead", filters=filters, fields=["name", by]):
		by_id.setdefault(r.get(by), r.get("name"))  # first lead per id (one per line in scope)

	results, found = [], 0
	for i, ident in enumerate(requested):
		lead_name = by_id.get(ident)
		if lead_name:
			results.append({"index": i, "status": "success",
				"data": _curate(frappe.get_doc("CRM Lead", lead_name), parent_fields, child_allow)})
			found += 1
		else:
			results.append({"index": i, "status": "error",
				"error": {"code": "not_found", "message": _("Lead not found")}})
	_ok(summary={"requested": len(requested), "found": found, "not_found": len(requested) - found},
		results=results)


@frappe.whitelist(methods=["GET"])
@_api(bulk=True)
def lead_list(**kwargs):
	"""List leads on the caller's line, filtered + paginated. Curated fields only, no
	children (use lead_get for the full record). Filters: status, created/updated date
	ranges, exact mobile_no — never arbitrary fields."""
	_user, mp, _is_sysmgr, parent_fields, _child_allow = _caller_fields()
	data = frappe.form_dict

	filters = []
	if mp:
		filters.append(["custom_vertical", "=", mp.vertical])
		filters.append(["custom_group", "=", mp.crm_group])
	for key, (field, op) in LIST_FILTERS.items():
		if data.get(key):
			filters.append([field, op, data.get(key)])
	if data.get("mobile_no"):
		filters.append(["mobile_no", "=", _norm_phone(data.get("mobile_no"))])

	cfg = _cfg()
	limit = min(cint(data.get("limit")) or cfg["list_default_page"], cfg["list_max_page"])
	offset = cint(data.get("offset") or data.get("limit_start"))
	denied = _meter_volume(limit, "read")  # read volume = the requested page size
	if denied is not None:
		return denied

	fields = list(dict.fromkeys(
		[*parent_fields, "name", "source", "custom_vertical", "custom_group", "custom_current_program"]
	))
	total = frappe.db.count("CRM Lead", filters)
	leads = frappe.get_all(
		"CRM Lead", filters=filters, fields=fields,
		limit_page_length=limit, limit_start=offset, order_by="modified desc",
	)
	_ok(action="fetched", data={"total": total, "count": len(leads), "offset": offset, "limit": limit, "leads": leads})
