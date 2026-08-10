"""Gated partner lead API — the ONLY surface external partners touch.

Partners are role-less System Users. Raw `/api/resource/*` returns 403 for them
(CRM's `org_hierarchy.has_lead_permission` blocks non-managers). These methods are
their entire contract; every one resolves the caller's `CRM Lead API Mapping` row:

  - Enabled mapping row: EXTERNAL partner. Routing (source, vertical, group and
    program) is FORCED from the row, reads and writes are scoped to that line, and
    the fields they may send or read are their ticked subset of the catalog.
  - System Manager, no mapping: TRUSTED internal (for example, MyTatvaCore). Sends
    routing in the body, full catalog, unscoped.
  - Neither: 403.

ONE set of endpoints serves every partner. What varies per partner is config on
their mapping row (routing and allowed-fields grid), never code.

IDENTITY. A lead is addressed by `name`, the CRM Lead primary key, returned when it was created.
`mobile_no` is ALSO a valid address here — and only here — because phone is the lead's natural key;
no other entity has one. Dedup is the CRM's own rule, (mobile_no, custom_vertical, custom_group), and
never a caller-supplied value: re-sending the same patient updates that lead, and a changed phone
number is a new patient and a new lead. `external_id` is the caller's own label (stored in
`custom_external_id`): echoed back on every read, never interpreted, never used to find a lead.
Retries are made safe with the Idempotency-Key header.

Singular:
  GET    lead_schema  the fields THIS caller may send or read, plus their routing
  GET    lead_get     one lead by `name` or `mobile_no`, scoped to the line
  POST   lead_create  create-or-update, deduped by phone + line + group; returns the record
  PUT    lead_update  update a lead by CRM `name` (the id POST returned)
  DELETE lead_delete  delete a lead by CRM `name`, scoped to the line

Bulk and query (each record enforced individually; partial success):
  POST   lead_create_bulk  {"leads": [...]}  (up to `bulk.max_per_call`)
  PUT    lead_update_bulk  {"updates": [{"name": ..., ...}]}  (up to `bulk.max_per_call`)
  DELETE lead_delete_bulk  {"names": [...]}  (up to `bulk.max_per_call`)
  POST   lead_get_bulk     {"names": [...]} or {"mobile_nos": [...]}  (up to `bulk.max_per_call`)
  GET    lead_list         curated filters and pagination, line-scoped
"""
import frappe
from frappe import _
from frappe.utils import cstr, now_datetime, today

from tatva_connect import automation
from tatva_connect.api._base import (
	ACTION_DELETED,
	ACTION_FETCHED,
	BEHAVIOR_OUTPUT_ONLY,
	EXTERNAL_ID_FIELD,
	_api,
	_bulk_read,
	_list_ok,
	_norm_phone,
	_ok,
	_page,
	_read_list,
	_resolve_caller,
	_run_bulk,
	_schema_ok,
	cast_declared,
	cast_declared_row,
	field_descriptor,
	is_writable,
	not_found_message,
	read_bulk_list,
	resolve_lead,
	stamp_external_id,
	throw_field,
	validate_external_id,
)
from tatva_connect.lead import keyvalue, multi_value
from tatva_connect.partner_api.doctype.crm_lead_section import crm_lead_section
from tatva_connect.taxonomy import labels

# ---------------------------------------------------------------------------
# The catalog (the platform superset a partner CAN be granted) is DATA, not code:
# it lives in the `CRM Lead API Field` master, one row per namespaced `section:fieldname`
# key, each naming the `CRM Lead Section` that routes it. The section — not the field —
# carries the target doctype, the child-table fieldname and the row key, once.
# `_catalog()` reads + caches both tables; everything below derives from them, so
# exposing a new partner field = one `CRM Lead API Field` row, no code change.
#
# The only structural code-side constant: the parent (non-child) section is "lead".
# A section whose `child_table_field` is set is a child section; one without is the
# CRM Lead row itself.
# ---------------------------------------------------------------------------
_CATALOG_CACHE_KEY = "tatva_connect:lead_api_catalog"
# Backstop TTL: the Partner::Catalog::cache toggle evicts instantly on edit when ON; this bounds staleness to 60 min even if that hook never fires (toggle off, or a missed event) — self-heals.
_CATALOG_CACHE_TTL_SEC = 60 * 60
PARENT_SECTION = "lead"


def _sections() -> dict:
	"""The lead sections, keyed by section_key (the doctype autonames on it)."""
	return {
		r.name: r
		for r in frappe.get_all(
			"CRM Lead Section",
			fields=["name", "title", "target_doctype", "child_table_field", "is_multi_row", "is_key_value",
			        *crm_lead_section.COLUMN_FIELDS],
			order_by="display_order asc",
		)
	}


def _build_catalog() -> dict:
	"""Read the `CRM Lead API Field` table into the structured catalog the API uses.
	Returns a dict (everything below is derived from these keys):
	  keys           ordered list of writable `section:fieldname` (sort_field=field_key)
	  key_set        set(keys)
	  labels         {field_key: label}  the catalog's own label, so no reader invents one
	  audit          [{fieldname, label}] reserved OUTPUT_ONLY lead fields, shown in the
	                 schema (marked) but never writable (see _base.RESERVED_FIELDS)
	  section_doctype  {section: target doctype}
	  section_child    {section: child-table fieldname}  (child sections only)
	  section_title    {section: display title}
	  section_key_field {section: fieldname}  (multi-row sections only — the section's
	                     row_key_field; its presence = multi-row)
	  section_key_value {section: the section row itself}  (key-value sections only — one row per field,
	                     where a field's `fieldname` addresses a ROW, not a column; the section names
	                     every column those rows are read through)
	  multi_value    {section: {fieldname: field_key}}  the fields that take MORE THAN ONE value, whose
	                 selections hang off the lead (tatva_connect.lead.multi_value) instead of a column
	The four section_* maps are the `CRM Lead Section` rows themselves: ONE row per section states
	its table, its target, its row key and its title, and no field row restates any of them.
	"""
	sections = _sections()
	keys, read_only, audit, field_labels = [], [], [], {}
	# Which fields are multi-value is `lead.multi_value`'s to say — this only projects that answer onto the section keys, cached because `_column_values` asks it once per incoming row.
	multi = {}
	for (section, fieldname), field_key in multi_value.declared().items():
		if section in sections:
			multi.setdefault(section, {})[fieldname] = field_key
	for r in frappe.get_all(
		"CRM Lead API Field",
		fields=["field_key", "label", "section", "fieldname"],
		order_by="field_key asc",
	):
		# A row whose section is not a lead section is not a lead field: these are the CRM Task rows
		# (promoted columns / JSON payload) the Smart Views composer reads, and the partner API
		# exposes lead fields only. Skip them so they never reach a partner's lead_schema.
		if r.section not in sections:
			continue
		# Grain routing fields (source / vertical / group / program) are FORCED from entitlement,
		# never partner-suppliable (see ROUTING_FIELDS). They ARE Smart-View catalog rows (so the
		# composer can filter/column by Product Line / Group / Program) but must never enter a
		# partner's writable lead_schema — skip them here. The Smart View path reads them directly.
		if r.fieldname in ROUTING_FIELDS:
			continue
		# Reserved audit fields (_base.RESERVED_FIELDS) stay OUT of the writable catalog so a partner
		# can never send them, but are surfaced (marked OUTPUT_ONLY) in lead_schema for discovery. They
		# live on the parent (lead) section; the Smart View composer reads the table directly, so its
		# own owner and creation columns are untouched.
		if not is_writable(r.fieldname):
			if r.section == PARENT_SECTION:
				audit.append({"fieldname": r.fieldname, "label": r.label or r.fieldname})
			continue
		field_labels[r.field_key] = r.label or r.fieldname
		# A key-value row is READ-ONLY: cataloguing a screening question shows it, it never grants a write.
		if sections[r.section].is_key_value:
			read_only.append(r.field_key)
			continue
		keys.append(r.field_key)
	return {
		"keys": keys,
		"key_set": set(keys),
		"read_only_keys": read_only,
		"labels": field_labels,
		"audit": audit,
		"section_doctype": {k: s.target_doctype for k, s in sections.items()},
		"section_child": {k: s.child_table_field for k, s in sections.items() if s.child_table_field},
		"section_title": {k: s.title for k, s in sections.items()},
		"section_key_field": {k: s.row_key_field for k, s in sections.items() if s.is_multi_row},
		"section_key_value": {k: s for k, s in sections.items() if s.is_key_value},
		"multi_value": multi,
	}


def _catalog() -> dict:
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
# DERIVED FROM THE SECTION: a section is multi-row iff it says so, and names the key
# field that addresses one of its rows. No hardcoded map, and no per-field copy.

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


def _section_of_child(cf):
	"""The section key a child-table fieldname belongs to, or "" — the ONE inversion of `section_child`.

	Three helpers walked that map for themselves and answered three different questions about the same
	row; the walk is written once here and each of them now just reads its own column off the section."""
	for section, child in _catalog()["section_child"].items():
		if child == cf:
			return section
	return ""


def catalog_section_title(child_fieldname):
	"""Readable section title for a child-table fieldname, e.g., 'custom_lab_profile'
	-> 'Lab' — used in child-write error messages ('report_date is required to
	identify a Lab row'). Falls back to the fieldname if the section is unknown."""
	section = _section_of_child(child_fieldname)
	return _catalog()["section_title"].get(section, child_fieldname)


def _child_key_field(cf):
	"""The row-key fieldname for a child-table fieldname, or None (single-row).
	The section states it; no catalog row carries a copy."""
	return _catalog()["section_key_field"].get(_section_of_child(cf))


def _child_key_value(cf):
	"""The `CRM Lead Section` behind a key-value child table, or None if it is not one.

	The section names every column its rows are read through — the identity, the answer, the label and
	the raw key the identity was derived from. A field row naming this section addresses one of its ROWS
	through that identity, never a column."""
	return _catalog()["section_key_value"].get(_section_of_child(cf))


# Multi-value fields: no column on the section, selections hang off the lead by (field_key, row_key). Below is address bookkeeping only — reading, resolving and writing one is `tatva_connect.lead.multi_value`.

def _multi_value_fields(section):
	"""{fieldname: field_key} for the multi-value fields of one section. Empty for every other."""
	return _catalog()["multi_value"].get(section) or {}


def _column_values(section, row):
	"""An incoming row as the COLUMNS it writes — the delete flag and every multi-value field dropped.

	A multi-value field is not a column, so setting one on a doc writes into nothing and the value is
	lost in silence. Dropped HERE, once, rather than at each of the three places that stage a row."""
	declared = _multi_value_fields(section)
	return {k: v for k, v in (row or {}).items() if k != "_delete" and k not in declared}


def _readable(doctype, row):
	"""A projected row as a human reads it: a Link at a COMPOSITE master holds a key built from the
	grain, so the label is published in its place. No field is named here — the schema says which
	fields those are, so the next composite field needs no code.

	The gate is `is_composite`, not "does the target have a title": `lead_owner` is a Link at `User`,
	whose title is a full name and whose key is an email. An email is the identifier a caller matches
	on; a full name is not unique and addresses nothing, so that one is published exactly as stored."""
	meta = frappe.get_meta(doctype)
	out = {}
	for fieldname, value in row.items():
		df = meta.get_field(fieldname)
		if df and df.fieldtype == "Link" and labels.is_composite(df.options):
			value = labels.shown(doctype, fieldname, value)
		out[fieldname] = value
	return out


def _read_multi_values(section, fieldnames, held, row_key):
	"""{fieldname: [value, ...]} for the multi-value fields of `section` this caller may read.

	`held` is one `multi_value.read_all` for the whole lead, passed in rather than re-read per row: a
	lead with twelve drug cycles is still one pass over one child table."""
	master = multi_value.value_field().options
	return {fn: [labels.label(v, master) for v in held.get((fk, cstr(row_key)), [])]
	        for fn, fk in _multi_value_fields(section).items() if fn in fieldnames}


def _stage_multi_values(doc, section, row, row_key):
	"""Stage the multi-value selections an incoming row carries, at that row's own address.

	Staged HERE and not in `_collect`, because the address is only settled once the upsert engine has
	resolved which row this is — a row that named no key is stamped with its arrival time.

	`row=None` is the delete: the row is going, so every selection hanging off it goes with it, through
	the same `replace` with nothing to put back. A field the caller did not send is left alone — an
	absent field is "not sent" here exactly as an empty string is everywhere else in this API."""
	for fieldname, field_key in _multi_value_fields(section).items():
		if row is None:
			multi_value.replace(doc, field_key, row_key, [])
		elif fieldname in row:
			multi_value.replace(doc, field_key, row_key, row.get(fieldname))


def _resolve_multi_values(section, row, grain):
	"""A collected row with every multi-value field's human values translated to composite picklist PKs.

	Not a second resolver: each value goes through `picklist.resolve_value`, the unit `resolve_row_links`
	itself is built from — same registry, same grain rule, same drop-and-log for a value this grain does
	not offer. The MASTER is read off the column the selections live in, and the FIELDNAME is the
	catalog's, because that is what the picklist category is derived from."""
	declared = _multi_value_fields(section)
	if not declared:
		return row
	from tatva_connect.taxonomy import picklist

	master = multi_value.value_field().options
	out = dict(row or {})
	for fieldname in declared:
		if fieldname not in out:
			continue
		sent = out[fieldname] if isinstance(out[fieldname], list) else [out[fieldname]]
		resolved = [picklist.resolve_value(master, v, grain, fieldname, multi_value.DOCTYPE) for v in sent]
		out[fieldname] = [v for v in resolved if v]
	return out


# -- helpers -----------------------------------------------------------------
# Entity-agnostic plumbing (_resolve_caller, _norm_phone, _ok/_fail, _classify,
# _api, _cfg, _run_bulk, _read_list, normalise_partner_response) now lives in
# tatva_connect.api._base and is imported above. Only lead-specific helpers remain
# in this module.

def _contract_name(user):
	"""The contract's id — the SAME row the gate resolved, never a second lookup: partner_user lost its
	unique when the key went composite, so two independent reads could take the grain from one row and
	the field allowlist from another."""
	ctx = getattr(frappe.local, "partner_ctx", None)
	if ctx and ctx[0] == user and ctx[1]:
		return ctx[1].get("name")
	return frappe.db.get_value("CRM Lead API Mapping", {"partner_user": user, "enabled": 1}, "name")


def _allowed_keys(user, has_mapping):
	"""The catalog keys THIS caller may use. Partner with a non-empty grid -> that
	subset (mobile_no always included). Empty grid, or System Manager -> full catalog."""
	cat = _catalog()
	if has_mapping:
		picked = frappe.get_all(
			"CRM Lead API Mapping Field",
			filters={"parent": _contract_name(user), "parenttype": "CRM Lead API Mapping"},
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
		filters={"parent": _contract_name(user), "parenttype": "CRM Lead API Mapping"},
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
	"""Pull ONLY this caller's allowed parent fields + child arrays from a payload, every value held to
	the type its schema DECLARES.

	Routing is included only for a trusted caller (allow_routing).

	The type rule and its wording live once, in `_base.cast_declared` — the same rule `field_spec.collect`
	holds a note, a call and a file to, and the same one the activity surface holds an answer to. Here it
	is reached at the lead's ONE ingestion seam, so every create, update, bulk record and Desk import row
	passes through it, and `lead_schema`'s published `type` is finally something a caller is held to.
	A child ARRAY is itself a declared Table, so the shape of the section is cast by the same call — that
	is where the hand-rolled parse (and orjson's own leaked sentence) used to live."""
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
	parent = cast_declared_row("CRM Lead", parent)

	lead_meta = frappe.get_meta("CRM Lead")
	children = {}
	for cf, allowed in child_allow.items():
		rows = data.get(cf)
		if not rows or not allowed:
			continue
		rows = cast_declared("CRM Lead", cf, rows)
		child_doctype = lead_meta.get_field(cf).options
		if _child_key_value(cf):
			# The section is the unit of grant: a key-value answer is identified by the question itself.
			children[cf] = [cast_declared_row(child_doctype, r) for r in rows if r]
			continue
		# The key_field of a multi-row child is the row's address: always keep it
		# (even if the partner's grid didn't tick it) plus the explicit _delete flag,
		# so the upsert engine can identify the row. Everything else is allow-gated.
		keep = set(allowed)
		key_field = _child_key_field(cf)
		if key_field:
			keep.add(key_field)
		keep.add("_delete")
		# An empty string is "not sent", never "erase this" — the same rule the parent fields above are
		# held to. A column carries one fact and cannot record that it was asked and left blank, so a
		# blank overwriting a stored value is data loss; `_merge_row` sets whatever reaches it. The key
		# field and the delete flag are addresses rather than values and are kept whatever they hold.
		children[cf] = [
			cast_declared_row(child_doctype, {
				k: v for k, v in (r or {}).items()
				if k in keep and (v not in (None, "") or k in (key_field, "_delete"))
			})
			for r in rows
		]
	return parent, children


def _resolve_picklists(parent, children, grain):
	"""Translate human picklist VALUES -> grain-scoped composite PKs on the collected parent + child
	dicts BEFORE they reach the doc. This is the ONE correct place: Frappe runs _validate_links()
	before any before_validate hook on insert/save, so a doc_event can't fix a Link — the ingestion
	path must resolve first. Rides the SAME taxonomy.picklist brain the lead_schema discovery
	advertises, so what a partner is TOLD they may send is exactly what is ACCEPTED. Shared by
	create + update. A multi-value field's LIST rides the same call, one value at a time."""
	from tatva_connect.taxonomy import picklist

	cm = frappe.get_meta("CRM Lead")
	parent = _resolve_multi_values(PARENT_SECTION, picklist.resolve_row_links("CRM Lead", parent, grain), grain)
	children = {
		cf: [_resolve_multi_values(_section_of_child(cf),
		                           picklist.resolve_row_links(cm.get_field(cf).options, r, grain), grain)
		     for r in rows]
		for cf, rows in children.items()
	}
	return parent, children


def _child_error(message, field):
	"""A child-row refusal, named by its child-table fieldname. One-arg spelling of the ONE field-naming
	refusal (`_base.throw_field`) — this module's callers pass a scalar field, that helper takes a list."""
	throw_field(message, [field])


def _merge_row(target, incoming, section):
	"""Overlay only the sent columns onto an existing child row (partial update)."""
	for k, v in _column_values(section, incoming).items():
		target.set(k, v)


def _apply_single_row(doc, cf, incoming, title, section):
	"""single-row child: merge sent fields onto the one row (create if none).
	A 2nd distinct incoming row is ambiguous -> 400."""
	if len(incoming) > 1:
		_child_error(_("{1} holds one row per lead and {0} arrived. Merge them into a single object "
		               "before sending.").format(len(incoming), title), cf)
	rows = doc.get(cf) or []
	if rows:
		_merge_row(rows[0], incoming[0], section)
	else:
		doc.append(cf, _column_values(section, incoming[0]))
	# The section keeps one row per lead, so its selections are addressed by the lead alone.
	_stage_multi_values(doc, section, incoming[0], "")


def _row_arrival_key(doc, cf, key_field):
	"""The key a row gets when its surface did not name one — the moment it arrived, typed to the column.

	A surface that KNOWS when the row happened sends it (Facebook sends Meta's `created_time`, a partner
	names its own). One that does not — an intake form, a rep — gets now. The alternative was a blank
	key, and a blank key on a multi-row section is no address at all: the section validator says so, the
	partner API could never target such a row, and every later write appended beside it for ever."""
	child_dt = doc.meta.get_field(cf).options
	df = frappe.get_meta(child_dt).get_field(key_field)
	return today() if (df and df.fieldtype == "Date") else now_datetime()


def _apply_multi_row(doc, cf, incoming, key_field, title, section):
	"""multi-row child, upsert-by-key. A row that names no key is stamped with its arrival time rather
	than refused — see `_row_arrival_key`. Match by key -> partial-merge that row; new key -> append;
	{key,_delete:true} -> drop that keyed row. Rows already on the doc that are not referenced -> untouched.

	A multi-value field is addressed by that same key, so it is staged here where the key is settled —
	and a deleted row takes its selections with it rather than leaving them at an address nothing owns."""
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
			# A delete still needs one: you cannot name the row to drop by not naming it.
			if (row or {}).get("_delete"):
				_child_error(
					_("A {1} row is deleted by its key and this row names none. Send `{0}` with the "
					  "value of the row to drop.").format(key_field, title), key_field
				)
			raw = _row_arrival_key(doc, cf, key_field)
			row = {**(row or {}), key_field: raw}
		key = cstr(raw)
		target = by_key.get(key)
		if row.get("_delete"):
			if target is not None:
				doc.remove(target)
				by_key.pop(key, None)
			_stage_multi_values(doc, section, None, key)
			continue
		if target is not None:
			_merge_row(target, row, section)
		else:
			new = doc.append(cf, _column_values(section, row))
			by_key[key] = new
		_stage_multi_values(doc, section, row, key)


def _already_present(rows, incoming):
	"""Whether a row identical on every field the caller sent is already on the doc."""
	return any(
		all(cstr(r.get(k)) == cstr(v) for k, v in (incoming or {}).items()) for r in rows
	)


def _apply_key_value(doc, cf, incoming, identity_field, section):
	"""key-value child: an answer is kept whenever it DIFFERS from what the question already holds, and
	every re-read that says the same thing changes nothing.

	Upserting on the identity alone destroyed the earlier answer when a patient answered the same
	question on a second campaign, with no record it had ever been different. A changed answer is itself
	the clinical fact, so it is appended and the readers show the newest. Re-reading a form is therefore
	idempotent: the row is only touched when the patient actually said something new."""
	value_field = _child_key_value(cf).value_field
	for row in incoming:
		rows = doc.get(cf) or []
		identity = cstr((row or {}).get(identity_field))
		answered = [r for r in rows if identity and cstr(r.get(identity_field)) == identity]
		newest = keyvalue.newest_first(answered)[0] if answered else None
		if newest is not None and cstr(newest.get(value_field)) == cstr((row or {}).get(value_field)):
			continue  # left alone, never merged: a merge rewrote which form asked it
		if identity or not _already_present(rows, row):
			doc.append(cf, _column_values(section, row))


def _apply_children(doc, children):
	"""Config-driven UPSERT-BY-KEY write engine (§3-4 of the child-table contract).
	Per child table, the section decides single-row vs multi-row + the key field;
	a partial write never wipes the other fields/rows already on the doc."""
	for cf, incoming in children.items():
		if not incoming:
			continue
		section = _section_of_child(cf)
		key_value = _child_key_value(cf)
		if key_value:
			_apply_key_value(doc, cf, incoming, key_value.row_key_field, section)
			continue
		key_field = _child_key_field(cf)
		title = catalog_section_title(cf)
		if key_field:
			_apply_multi_row(doc, cf, incoming, key_field, title, section)
		else:
			_apply_single_row(doc, cf, incoming, title, section)


def _apply_parent(doc, parent):
	"""The lead's OWN collected fields onto the doc — the twin of `_apply_children`, and the ONE parent
	write, so a create, a merge and an update cannot come to differ.

	A multi-value field on the lead section is routed to its resolver at the lead's own address (blank),
	because it has no column: `doc.update` would set an attribute nothing persists."""
	doc.update(_column_values(PARENT_SECTION, parent))
	_stage_multi_values(doc, PARENT_SECTION, parent, "")


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

# The PUBLIC shape of a key-value row. Stable on the wire whatever the section names its columns:
# the declaration says where to read each one FROM, this says what a partner sees it AS.
KEY_VALUE_VIEW = ("question", "label", "value")


def _key_value_columns(section):
	"""The columns behind KEY_VALUE_VIEW, in that order — named once, read by the projection AND the
	schema, so what a partner is told and what a partner receives cannot describe different columns."""
	return (section.question_field, section.label_field, section.value_field)


def _key_value_descriptor(public_name, df):
	"""One KEY_VALUE_VIEW field, typed from the column it is read out of and marked never-writable."""
	d = field_descriptor(public_name, df.label if df else public_name, df.fieldtype if df else "Data")
	d["behavior"] = BEHAVIOR_OUTPUT_ONLY
	d["required"] = False
	return d


def _catalogued_answers(doc, cf, section):
	"""A key-value section's rows, read the way the section says to read them.

	Only the questions an operator has CATALOGUED are returned — that row is the grant, and it is the
	only one there can be, because a key-value field addresses a row rather than a column and so can
	never enter a partner's own field grid. Read-only by construction: `keys` does not carry them, so
	nothing here is writable by anybody."""
	shown = {k.partition(":")[2] for k in _catalog()["read_only_keys"]}
	if not shown:
		return []
	source = _key_value_columns(section)
	return [
		dict(zip(KEY_VALUE_VIEW, [row.get(c) for c in source], strict=True), name=row.get("name"))
		for row in (doc.get(cf) or [])
		if row.get(section.row_key_field) in shown
	]


def _multi_value_descriptor(fieldname, field_key, mp):
	"""One multi-value field, described from its catalog row and from the column it really stores in.

	There is no docfield to read: the section's doctype has no column for this field, which is the whole
	point. The label is the catalog's — the same one every other reader shows — and the type is the one
	`CRM Lead Multi Value.value` declares, so the vocabulary advertised is the vocabulary accepted, from
	the SAME registry the ingestion resolver dispatches through. A list, so never `required`."""
	df = multi_value.value_field()
	allowed_values = None
	if mp:
		from tatva_connect.taxonomy import picklist

		allowed_values = picklist.values_for(df.options, (mp.vertical, mp.crm_group, mp.program or ""), fieldname)
	return field_descriptor(fieldname, _catalog()["labels"].get(field_key) or fieldname,
	                        df.fieldtype, False, df.options, allowed_values or None, multi=True)


def _audit_fieldnames():
	"""The reserved OUTPUT_ONLY lead fieldnames, deduped in catalog order — read once, projected by the
	response and selected by the list, so a field `lead_schema` advertises is a field a response carries.

	Deduped because a duplicate `CRM Lead API Field` row is OPERATOR data: two rows may name one field,
	and what seeds decide code does not re-decide — it just must not answer twice for one key."""
	return list(dict.fromkeys(a["fieldname"] for a in _catalog()["audit"]))


def _curate(doc, parent_fields, child_allow):
	"""A lead as only the caller's allowed fields (+ name, the caller's own external_id label, the
	OUTPUT_ONLY audit fields and read-only routing). The ONE lead projection: every read AND every write
	returns this, so a create, an update and a get can never hand back different shapes.

	The VALUES a create returned once disagreed with the ones a get returned, because a create projected
	the in-memory doc where a coerced value had not round-tripped: `"hba1c": "high"` was handed back while
	the row held 0.0. `cast_declared` closes that at the seam instead — a value the caller SENT now reaches
	the doc already in its declared type, so memory and row agree and no re-read is needed. Measured, the
	only residue is an UNSET numeric reading None here and 0.0 once stored, which is the accepted
	NOT NULL DEFAULT 0 behaviour of the column and not worth a full doc read on every write.

	The audit fields are projected but never writable — `_build_catalog` routes a reserved field into
	`audit` and out of `keys`, so read-only cannot become invisible. `lead_schema` has advertised
	`owner`, `creation`, `modified` and `lead_owner` as OUTPUT_ONLY since it was written, and until this
	no response carried one of them.

	A multi-value field has no column to project off, so its selections are read from the lead's own
	`multi_value` rows — once for the whole lead, then addressed per row."""
	held = multi_value.read_all(doc)
	out = _readable("CRM Lead", {fn: doc.get(fn) for fn in [*parent_fields, *_audit_fieldnames()]})
	out.update(_read_multi_values(PARENT_SECTION, parent_fields, held, ""))
	out.update({
		"name": doc.name, "external_id": doc.get(EXTERNAL_ID_FIELD),
		"source": doc.source, "custom_vertical": doc.custom_vertical,
		"custom_group": doc.custom_group, "custom_current_program": doc.custom_current_program,
	})
	for section_key, section in _catalog()["section_key_value"].items():
		cf = _catalog()["section_child"].get(section_key)
		answers = _catalogued_answers(doc, cf, section) if cf else []
		if answers:
			out[cf] = answers
	for cf, allowed in child_allow.items():
		if allowed:
			# Return the caller's allowed fields + our row id (name) + the key field (the row's address per the contract — always present even if not ticked).
			key_field = _child_key_field(cf)
			section = _section_of_child(cf)
			keys = list(allowed)
			if key_field and key_field not in keys:
				keys.append(key_field)
			out[cf] = [
				dict(_readable(_catalog()["section_doctype"][section], {k: r.get(k) for k in keys}),
				     **_read_multi_values(section, keys, held, r.get(key_field) if key_field else ""),
				     name=r.get("name"))
				for r in (doc.get(cf) or [])
			]
	return out


# -- the lead's API contract — ONE source of truth for what the API actually enforces, so
# lead_schema's `required` flags match behaviour (and can't drift from it).
LEAD_IDENTITY = "mobile_no"   # the dedup anchor -> required
_NAMELESS = "(no name)"       # placeholder for a lead sent without a name (the doctype requires one)
# Parent fields the caller may OMIT (reported not-required, overriding the doctype's reqd flag):
# first_name -> we fill the placeholder; status -> CRM's CRM Lead controller defaults it.
LEAD_OPTIONAL = ("first_name", "status")
_LEAD_REQUIRED = {LEAD_IDENTITY: True, **{fn: False for fn in LEAD_OPTIONAL}}


# -- scope helper ------------------------------------------------------------

def _scoped_lead(name, mp, is_sysmgr):
	"""Resolve one lead by name, grain-scoped — the lead's `_scoped_*` gate, matching the one every
	other entity uses (_scoped_task, _scoped_call, _scoped_file).

	Delegates to the SHARED resolve_lead brain rather than re-implementing the grain filter. Both the
	update and the delete used to hand-roll `custom_vertical != mp.vertical or custom_group !=
	mp.crm_group` — the same rule, written twice, in a file whose siblings all call one resolver. A
	change to what a grain means would have had to be made in three places and would have been missed
	in one. Missing and out-of-scope still return the SAME generic not-found: resolve_lead is where
	that guarantee already lives."""
	return resolve_lead(mp, is_sysmgr, {"lead": name})


# -- per-record core (shared by singular + bulk) -----------------------------

def _upsert_one(item, mp, is_sysmgr, parent_fields, child_allow, allowed_programs=None):
	"""Create-or-upsert one lead from a dict. Returns (doc, action)."""
	mobile = _norm_phone(item.get(LEAD_IDENTITY))
	if not mobile:
		throw_field(_(
			"A lead is identified by its phone number and this record carries none. Send `{0}` in E.164 "
			"(for example +919876543210)."
		).format(LEAD_IDENTITY), [LEAD_IDENTITY])
	validate_external_id("CRM Lead", item.get("external_id"))
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
	anchor = {"mobile_no": mobile, "custom_vertical": anchor_vertical, "custom_group": anchor_group}
	existing = frappe.db.get_value("CRM Lead", anchor, "name")
	if existing:
		return _merge_onto(existing, parent, children, mp, program, open_program, item)

	parent.setdefault("first_name", _NAMELESS)  # status is left for CRM's controller to default
	doc = frappe.new_doc("CRM Lead")
	_apply_parent(doc, parent)
	_apply_children(doc, children)
	if mp:
		_force_routing(doc, mp)
	if open_program:
		doc.custom_current_program = program

	# The lookup above and dedup_guard's are both non-locking reads, so two concurrent creates for one
	# patient miss both and reach here. The UNIQUE index is the real gate; when it fires the other
	# request has committed, so fold onto its row. UniqueValidationError(ValidationError) is an index
	# violation, DuplicateEntryError(NameError) a doc-name collision — unrelated branches, catch both.
	sp = f"lead_insert_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(sp)
	try:
		doc.insert(ignore_permissions=True)  # authz-ok: tier-b — gated by _resolve_caller + the grain filter, before the save
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		frappe.db.rollback(save_point=sp)
		winner = frappe.db.get_value("CRM Lead", anchor, "name")
		if not winner:
			raise  # the clash was on some OTHER unique key (facebook_lead_id, ...) — not ours to absorb
		return _merge_onto(winner, parent, children, mp, program, open_program, item)

	_stamp_label(doc, item)
	return doc, "created"


def _merge_onto(name, parent, children, mp, program, open_program, item):
	"""Overlay the payload onto an existing lead. The one update path — taken both when the dedup
	lookup finds it and when the unique index catches a race, so the two converge."""
	doc = frappe.get_doc("CRM Lead", name)
	_apply_parent(doc, parent)
	_apply_children(doc, children)
	if mp:
		_force_routing(doc, mp)
	if open_program and program:
		doc.custom_current_program = program
	doc.save(ignore_permissions=True)  # authz-ok: tier-b — gated by _resolve_caller + the grain filter, before the save
	_stamp_label(doc, item)
	return doc, "updated"


def _stamp_label(doc, item):
	"""Store the caller's label, when one was sent, and mirror it onto the in-memory doc so a caller
	holding that doc reads what the row now holds. A label is never identity."""
	external_id = item.get("external_id")
	if external_id is None:
		return
	stamp_external_id("CRM Lead", doc.name, external_id)
	doc.set(EXTERNAL_ID_FIELD, external_id)


def _update_one(name, item, mp, is_sysmgr, parent_fields, child_allow, allowed_programs=None):
	"""Update one lead by CRM name. Returns (doc, 'updated'). Scope-checked for partners."""
	if not name:
		throw_field(_(
			"No lead was named. Send `name`, the CRM Lead id returned when the lead was created; an "
			"update addresses a lead by id, never by phone number."
		), ["name"])
	doc = frappe.get_doc("CRM Lead", _scoped_lead(name, mp, is_sysmgr))
	validate_external_id("CRM Lead", item.get("external_id"))
	parent, children = _collect(item, parent_fields, child_allow, allow_routing=bool(is_sysmgr and not mp))

	# Program is a MUTABLE ATTRIBUTE, so an update transitions it — through the SAME resolver the
	# create uses, so both paths validate against the key's allowed_programs identically. An update
	# used to drop it silently: the caller got 200 and the program never changed, while lead_schema
	# advertised the field as writable.
	program = _resolve_program(item, mp, allowed_programs) if item.get("custom_current_program") else None
	open_program = bool(mp and not mp.program)

	grain = (
		(mp.vertical if mp else doc.custom_vertical) or "",
		(mp.crm_group if mp else doc.custom_group) or "",
		(program or doc.custom_current_program or ""),
	)
	parent, children = _resolve_picklists(parent, children, grain)
	_apply_parent(doc, parent)
	_apply_children(doc, children)
	if mp:
		_force_routing(doc, mp)
	if open_program and program:
		doc.custom_current_program = program
	doc.save(ignore_permissions=True)  # authz-ok: tier-b — gated by _resolve_caller + the grain filter, before the save
	_stamp_label(doc, item)
	return doc, "updated"


def _delete_one(name, mp, is_sysmgr=False):
	"""Delete one lead by CRM name. Scope-checked for partners."""
	if not name:
		throw_field(_(
			"No lead was named. Send `name`, the CRM Lead id returned when the lead was created; a "
			"delete addresses a lead by id, never by phone number."
		), ["name"])
	frappe.delete_doc("CRM Lead", _scoped_lead(name, mp, is_sysmgr), ignore_permissions=True)  # authz-ok: tier-b — gated by _scoped_lead, before the delete


# -- singular endpoints ------------------------------------------------------

# Fieldtypes for Frappe standard fields, which are not DocFields (meta.get_field returns None).
_STD_FIELD_TYPES = {"name": "Data", "owner": "Link", "creation": "Datetime", "modified": "Datetime", "modified_by": "Link"}


def _audit_field(fieldname, label, meta):
	"""OUTPUT_ONLY descriptor for a reserved audit field (discovery only, never writable).
	Frappe standard fields are not DocFields, so fall back to _STD_FIELD_TYPES for the type."""
	f = meta.get_field(fieldname)
	ftype = f.fieldtype if f else _STD_FIELD_TYPES.get(fieldname, "Data")
	return field_descriptor(fieldname, (f.label if f else None) or label, ftype, options=(f.options if f else None))


@frappe.whitelist(methods=["GET"])
@_api(read=True)
def lead_schema(**_kwargs):
	"""Discovery: the fields THIS caller may send/read + their routing mode.
	Two partners hitting this get different field lists — driven by their grid."""
	user, mp, _is_sysmgr, parent_fields, child_allow = _caller_fields()

	def _describe_section_fields(doctype, section_fields, required_override=None, section=None):
		"""Field dicts for a section. `required_override` ({fieldname: bool}) reports the API's
		actual contract instead of the doctype's `reqd` flag — used for the parent (identity
		required, defaulted fields not) and a child key_field (required to address its row).

		A multi-value field has no column on `doctype`, so it would fall out of a schema that only reads
		the meta — advertised from the catalog and from the column its selections really live in, or a
		partner would be sending and reading a field discovery never mentions."""
		required_override = required_override or {}
		m = frappe.get_meta(doctype)
		declared_multi = _multi_value_fields(section)
		entries = []
		for fn in section_fields:
			if fn in declared_multi:
				entries.append(_multi_value_descriptor(fn, declared_multi[fn], mp))
				continue
			f = m.get_field(fn)
			if not f:
				continue
			required = required_override.get(fn, bool(f.reqd))
			# Controlled vocabulary for any grain-scoped composite-PK Link (picklist, stage, ...):
			# advertise the exact human values this caller may send, from the SAME registry the
			# ingestion resolver dispatches through, so discovery equals ingestion. Only when the
			# grain is fixed (a partner mapping); a trusted caller supplies its own grain.
			allowed_values = None
			if mp and f.fieldtype == "Link":
				from tatva_connect.taxonomy import picklist

				allowed_values = picklist.values_for(f.options, (mp.vertical, mp.crm_group, mp.program or ""), fn)
			entries.append(field_descriptor(fn, f.label, f.fieldtype, required, f.options, allowed_values or None))
		return entries

	cat = _catalog()
	children = {}
	# A key-value section is advertised exactly as `_curate` returns it: the catalogued questions, read
	# through the columns the section names, every one OUTPUT_ONLY. Nothing to send, so no key_field.
	for section_key, section in cat["section_key_value"].items():
		cf = cat["section_child"].get(section_key)
		if not (cf and cat["read_only_keys"]):
			continue
		meta = frappe.get_meta(section.target_doctype)
		children[cf] = {
			"multi_row": True,
			"key_field": None,
			"fields": [
				_key_value_descriptor(public, meta.get_field(column))
				for public, column in zip(KEY_VALUE_VIEW, _key_value_columns(section), strict=True)
			],
		}
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
			"fields": _describe_section_fields(cat["section_doctype"][section], fields,
			                   required_override={key_field: True} if key_field else None,
			                   section=section),
		}

	# The lead's writable fields + the caller's own external_id label + the OUTPUT_ONLY audit fields
	# (discoverable, never writable — Frappe and the assignment rule set those).
	m_lead = frappe.get_meta("CRM Lead")
	fields = _describe_section_fields("CRM Lead", parent_fields, required_override=_LEAD_REQUIRED,
	                                 section=PARENT_SECTION)
	fields.append(field_descriptor("external_id", "External ID", "Data", required=False))
	fields += [_audit_field(a["fieldname"], a["label"], m_lead) for a in cat["audit"]]

	if mp and not mp.program:
		# Open-program key: line + group forced; program mode is LIST if the key has an
		# allowed_programs set, else NONE. Both derived from config, no hardcoding.
		ap = _allowed_programs(user, True)
		routing = {
			"mode": "list" if ap else "none",
			"source": mp.source, "vertical": mp.vertical, "group": mp.crm_group,
			"program": None,
			"allowed_programs": ap,
			"program_required": bool(ap),
			"note": (
				"Line and group are fixed. custom_current_program is sent from allowed_programs on "
				"every lead. Program is a mutable attribute, NOT identity: the same patient on a "
				"new program is the SAME lead (a transition)."
			) if ap else "Line and group are fixed. This key uses no program.",
		}
	elif mp:
		routing = {
			"mode": "forced", "source": mp.source, "vertical": mp.vertical,
			"group": mp.crm_group, "program": mp.program,
			"note": "Routing is fixed. Any routing fields sent in the body are ignored.",
		}
	else:
		routing = {
			"mode": "caller-supplied", "fields": list(ROUTING_FIELDS),
			"note": "Trusted caller: these routing fields are sent in the body.",
		}

	_schema_ok(
		"lead",
		dedup=(
			"A lead is unique per (mobile_no, product line, group) — the CRM's own rule, never an "
			"`external_id`. Re-sending the same patient updates that lead. Program is NOT part of "
			"identity: the same patient sent with a different program transitions the SAME lead. A "
			"changed phone number is a new patient and mints a new lead."
		),
		fields=fields,
		children=children,
		child_write=(
			"Each child is sent as a JSON array under its key, e.g. "
			"custom_lab_profile=[{\"report_date\":\"2026-01-15\", ...}]. Multi-row children are "
			"upsert-by-key on key_field: a new key adds a row, the same key updates that row (only "
			"sent fields change), and rows that are omitted are left untouched. Single-row children "
			"merge onto the one row. A row is removed with {<key_field>, \"_delete\": true}."
		),
		routing=routing,
		list_filters=[*list(LIST_FILTERS.keys()), "mobile_no"],
	)


def _read_one(ident, by, mp, parent_fields, child_allow):
	"""Load ONE lead by `name` or `mobile_no` -> the curated payload. The per-record loader both
	lead_get and lead_get_bulk call, so a single read and a bulk read can never diverge. Missing AND
	out-of-scope raise the SAME generic not-found (no probing)."""
	filters = {by: _norm_phone(ident) if by == "mobile_no" else ident}
	if mp:
		filters["custom_vertical"] = mp.vertical
		filters["custom_group"] = mp.crm_group
	lead_name = frappe.db.get_value("CRM Lead", filters, "name")
	if not lead_name:
		# ONE answer for missing and for out-of-scope: a refusal must never confirm that an id exists.
		throw_field(not_found_message("lead", by, hint=_(
			"Check the value against a lead_list response, or create the lead with lead_create first."
		)), [by], frappe.DoesNotExistError)
	return _curate(frappe.get_doc("CRM Lead", lead_name), parent_fields, child_allow)


@frappe.whitelist(methods=["GET"])
@_api(read=True)
def lead_get(**_kwargs):
	"""Read one lead by `name` or `mobile_no` (phone is the lead's natural key, so it is a valid
	address here — no other entity has one). A partner only sees leads on their line, and only
	their allowed fields."""
	_user, mp, _is_sysmgr, parent_fields, child_allow = _caller_fields()
	data = frappe.form_dict
	if not (data.get("name") or data.get("mobile_no")):
		throw_field(_(
			"No lead was named. Send `name` (the CRM Lead id) or `mobile_no` (the patient's number in "
			"E.164) — a lead is readable by either."
		), ["name", "mobile_no"])
	by = "name" if data.get("name") else "mobile_no"
	_ok(action=ACTION_FETCHED, data=_read_one(data.get(by), by, mp, parent_fields, child_allow))


@frappe.whitelist(methods=["POST"])
@_api
def lead_create(**_kwargs):
	"""Create-or-update a lead, deduped by phone + line + group (the CRM's own rule). Returns the
	full record, including the `name` to address it by from now on."""
	user, mp, is_sysmgr, parent_fields, child_allow = _caller_fields()
	allowed_programs = _allowed_programs(user, bool(mp))
	doc, action = _upsert_one(frappe.form_dict, mp, is_sysmgr, parent_fields, child_allow, allowed_programs)
	_ok(action=action, data=_curate(doc, parent_fields, child_allow))


@frappe.whitelist(methods=["PUT"])
@_api
def lead_update(**_kwargs):
	"""Update a lead by CRM `name`. Partner scope-checked; can't move it to another line."""
	user, mp, is_sysmgr, parent_fields, child_allow = _caller_fields()
	allowed_programs = _allowed_programs(user, bool(mp))
	doc, action = _update_one(frappe.form_dict.get("name"), frappe.form_dict, mp, is_sysmgr,
	                          parent_fields, child_allow, allowed_programs)
	_ok(action=action, data=_curate(doc, parent_fields, child_allow))


@frappe.whitelist(methods=["DELETE"])
@_api
def lead_delete(**_kwargs):
	"""Delete a lead by CRM `name`. Partner scope-checked (own line only). A lead with
	linked activity raises LinkExistsError — so a partner can't nuke a worked lead."""
	_user, mp, is_sysmgr, _parent_fields, _child_allow = _caller_fields()
	name = frappe.form_dict.get("name")
	_delete_one(name, mp, is_sysmgr)
	_ok(action=ACTION_DELETED, data={"name": name})


# -- bulk / query endpoints --------------------------------------------------

@frappe.whitelist(methods=["POST"])
@_api(bulk=True)
def lead_create_bulk(**_kwargs):
	"""Create-or-update many leads. Body: {"leads":[{...}, ...]} (up to `bulk.max_per_call`). Partial success."""
	user, mp, is_sysmgr, parent_fields, child_allow = _caller_fields()
	allowed_programs = _allowed_programs(user, bool(mp))
	leads = read_bulk_list("lead", "create")
	return _run_bulk(leads, bulk_creator(user, mp, is_sysmgr, parent_fields, child_allow, allowed_programs))


def bulk_creator(user, mp, is_sysmgr, parent_fields, child_allow, allowed_programs):
	"""The per-record create closure, shared by the sync bulk endpoint and the async worker (one brain)."""
	def one(i, item):
		doc, action = _upsert_one(item, mp, is_sysmgr, parent_fields, child_allow, allowed_programs)
		return {"index": i, "status": "success", "action": action,
		        "data": _curate(doc, parent_fields, child_allow)}
	return one


@frappe.whitelist(methods=["PUT"])
@_api(bulk=True)
def lead_update_bulk(**_kwargs):
	"""Update many leads. Body: {"updates":[{"name":..,..fields}, ...]} (up to `bulk.max_per_call`). Partial success."""
	user, mp, is_sysmgr, parent_fields, child_allow = _caller_fields()
	allowed_programs = _allowed_programs(user, bool(mp))
	updates = read_bulk_list("lead", "update")

	def one(i, item):
		doc, action = _update_one((item or {}).get("name"), item, mp, is_sysmgr, parent_fields,
		                          child_allow, allowed_programs)
		return {"index": i, "status": "success", "action": action,
		        "data": _curate(doc, parent_fields, child_allow)}

	return _run_bulk(updates, one)


@frappe.whitelist(methods=["DELETE"])
@_api(bulk=True)
def lead_delete_bulk(**_kwargs):
	"""Delete many leads. Body: {"names":[...]} (up to `bulk.max_per_call`). Partial success."""
	_user, mp, is_sysmgr, _parent_fields, _child_allow = _caller_fields()
	names = read_bulk_list("lead", "delete")

	def one(i, name):
		_delete_one(name, mp, is_sysmgr)
		return {"index": i, "status": "success", "action": ACTION_DELETED, "data": {"name": name}}

	return _run_bulk(names, one)


@frappe.whitelist(methods=["POST"])
@_api(bulk=True, read=True)
def lead_get_bulk(**_kwargs):
	"""Read many leads by `names` OR `mobile_nos` (up to `bulk.max_per_call`). Input-ordered; out-of-scope/unknown ids
	are reported not_found in place."""
	_user, mp, _is_sysmgr, parent_fields, child_allow = _caller_fields()
	data = frappe.form_dict
	names = _read_list(data, "names")
	mobiles = _read_list(data, "mobile_nos")
	if not names and not mobiles:
		throw_field(_(
			"No leads were named. Send `names` (a JSON array of CRM Lead ids) or `mobile_nos` (a JSON "
			"array of numbers in E.164) — a batch read takes either."
		), ["names", "mobile_nos"])

	by = "name" if names else "mobile_no"
	requested = list(names or mobiles or [])
	return _bulk_read(requested, lambda ident: _read_one(ident, by, mp, parent_fields, child_allow))


@frappe.whitelist(methods=["GET"])
@_api(bulk=True, read=True)
def lead_list(**_kwargs):
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

	limit, offset = _page(data)

	# SELECT only real CRM Lead columns: a catalog fieldname that is a Smart-View-only alias (no column)
	# would otherwise break the SQL. lead_schema/_curate already filter defensively via meta.get_field.
	# The audit fields ride the SAME list _curate projects, so a field a partner reads on lead_get does
	# not disappear on lead_list; a framework standard field (owner, creation) is not a docfield and is
	# dropped here exactly as it always was.
	m = frappe.get_meta("CRM Lead")
	safe_fields = [f for f in [*parent_fields, *_audit_fieldnames()] if m.has_field(f)]
	fields = list(dict.fromkeys(
		[*safe_fields, "name", EXTERNAL_ID_FIELD, "source", "custom_vertical", "custom_group",
		 "custom_current_program"]
	))
	total = frappe.db.count("CRM Lead", filters)
	leads = frappe.get_all(
		"CRM Lead", filters=filters, fields=fields,
		limit_page_length=limit, limit_start=offset, order_by="modified desc",
	)
	# Echo the caller's own label under the partner-facing key, not the raw column name, and read a
	# composite Link the SAME way `_curate` does — a page and a single read must agree about a VALUE, not
	# just about which fields it carries.
	leads = [_readable("CRM Lead", row) for row in leads]
	for row in leads:
		row["external_id"] = row.pop(EXTERNAL_ID_FIELD, None)
	_list_ok("leads", leads, total, offset, limit)
