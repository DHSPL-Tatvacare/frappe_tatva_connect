"""Gated partner lead API — the ONLY surface external partners touch.

Partners are role-less System Users. Raw `/api/resource/*` returns 403 for them
(crm's `org_hierarchy.has_lead_permission` blocks non-managers). These methods are
their entire contract — every one resolves the caller's `Lead API Mapping` row:

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
from frappe.utils import cint

# ---------------------------------------------------------------------------
# CATALOG = the platform superset a partner CAN be granted. Single source of
# truth. Keys are namespaced `section:fieldname`. The `Lead API Field` master is
# seeded from this (patch), so the per-partner grid is a dropdown of these keys.
# ---------------------------------------------------------------------------
SECTION_DOCTYPE = {
	"lead": "CRM Lead",
	"plan": "CRM Plan Profile",
	"lab": "CRM Lab Profile",
}
SECTION_CHILD = {  # child sections -> the CRM Lead child-table fieldname they write into
	"plan": "custom_plan_profile",
	"lab": "custom_lab_profile",
}
CATALOG = [
	"lead:mobile_no", "lead:first_name", "lead:last_name", "lead:email", "lead:custom_dob",
	"lead:custom_gender", "lead:custom_alternate_number", "lead:custom_city",
	"lead:custom_preferred_language", "lead:custom_patient_id", "lead:status",
	"plan:policy_number", "plan:member_id", "plan:payment_link", "plan:plan_name",
	"plan:priority_text", "plan:substage",
	"lab:hba1c", "lab:fbs", "lab:ldl", "lab:hdl", "lab:vldl", "lab:total_cholesterol",
	"lab:triglycerides", "lab:tsh", "lab:creatinine", "lab:egfr", "lab:alt_sgpt", "lab:ggt",
	"lab:height_feet", "lab:weight_kg", "lab:primary_condition", "lab:comorbid_conditions",
	"lab:report_date",
]
CATALOG_SET = set(CATALOG)
SECTION_TITLE = {"lead": "Lead", "plan": "Plan", "lab": "Lab"}

# Forced for partners, accepted from a trusted System Manager. Never a catalog field.
ROUTING_FIELDS = ("source", "custom_vertical", "custom_group", "custom_current_program")

# Limits
BULK_MAX = 100          # records per bulk call
LIST_DEFAULT = 20       # default page size
LIST_MAX = 200          # max page size

# lead_list: only these (safe, indexed) filters are honoured. NOT arbitrary fields.
#   key in request -> (CRM Lead field, operator)
LIST_FILTERS = {
	"status": ("status", "="),
	"created_after": ("creation", ">="),
	"created_before": ("creation", "<="),
	"updated_after": ("modified", ">="),
	"updated_before": ("modified", "<="),
}


def catalog_label(key):
	"""Readable label for a catalog key, e.g. 'lab:hba1c' -> 'Lab — HbA1c'."""
	section, _, fieldname = key.partition(":")
	doctype = SECTION_DOCTYPE.get(section)
	f = frappe.get_meta(doctype).get_field(fieldname) if doctype else None
	label = f.label if f else fieldname
	return "{0} — {1}".format(SECTION_TITLE.get(section, section), label)


# -- helpers -----------------------------------------------------------------

def _norm_phone(raw):
	if not raw:
		return raw
	d = "".join(c for c in str(raw) if c.isdigit())
	return ("+91" + d) if len(d) == 10 else (("+" + d) if d else raw)


def _resolve_caller():
	"""(user, mapping-or-None, is_sysmgr). Raises 403 if neither partner nor sysmgr.
	The mapping row's name == partner_user (autoname field:partner_user)."""
	user = frappe.session.user
	mp = frappe.db.get_value(
		"Lead API Mapping", {"partner_user": user, "enabled": 1},
		["source", "vertical", "crm_group", "program"], as_dict=True,
	)
	is_sysmgr = "System Manager" in frappe.get_roles(user)
	if not mp and not is_sysmgr:
		frappe.throw(_("Not authorised: no Lead API Mapping for {0}").format(user), frappe.PermissionError)
	return user, mp, is_sysmgr


def _allowed_keys(user, has_mapping):
	"""The catalog keys THIS caller may use. Partner with a non-empty grid -> that
	subset (mobile_no always included). Empty grid, or System Manager -> full catalog."""
	if has_mapping:
		picked = frappe.get_all(
			"Lead API Mapping Field",
			filters={"parent": user, "parenttype": "Lead API Mapping"},
			pluck="field",
		)
		picked = {k for k in picked if k in CATALOG_SET}
		if picked:
			picked.add("lead:mobile_no")
			return [k for k in CATALOG if k in picked]
	return list(CATALOG)


def _split_keys(keys):
	"""namespaced keys -> (parent_fieldnames, {child_fieldname: [fieldnames]})."""
	parent_fields = []
	child_allow = {cf: [] for cf in SECTION_CHILD.values()}
	for k in keys:
		section, _, fieldname = k.partition(":")
		if section == "lead":
			parent_fields.append(fieldname)
		elif section in SECTION_CHILD:
			child_allow[SECTION_CHILD[section]].append(fieldname)
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
		children[cf] = [{k: v for k, v in (r or {}).items() if k in allowed} for r in rows]
	return parent, children


def _apply_children(doc, children):
	"""Merge child rows by field: overlay the sent fields onto row[0], append extras.
	A partial child write never wipes the other fields already in that row."""
	for cf, incoming in children.items():
		if not incoming:
			continue
		rows = doc.get(cf) or []
		if rows:
			target = rows[0]
			for k, v in (incoming[0] or {}).items():
				target.set(k, v)
			for extra in incoming[1:]:
				doc.append(cf, extra)
		else:
			for r in incoming:
				doc.append(cf, r)


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


def _result(doc, action):
	return {
		"ok": True, "action": action, "name": doc.name,
		"source": doc.source, "vertical": doc.custom_vertical,
		"group": doc.custom_group, "program": doc.custom_current_program,
	}


def _curate(doc, parent_fields, child_allow):
	"""A lead as only the caller's allowed fields (+ name + read-only routing)."""
	out = {fn: doc.get(fn) for fn in parent_fields}
	out.update({
		"name": doc.name, "source": doc.source, "custom_vertical": doc.custom_vertical,
		"custom_group": doc.custom_group, "custom_current_program": doc.custom_current_program,
	})
	for cf, allowed in child_allow.items():
		if allowed:
			out[cf] = [{k: r.get(k) for k in allowed} for r in (doc.get(cf) or [])]
	return out


# -- per-record core (shared by singular + bulk) -----------------------------

def _upsert_one(item, mp, is_sysmgr, parent_fields, child_allow):
	"""Create-or-upsert one lead from a dict. Returns (doc, action)."""
	mobile = _norm_phone(item.get("mobile_no"))
	if not mobile:
		frappe.throw(_("mobile_no is required"))
	parent, children = _collect(item, parent_fields, child_allow, allow_routing=bool(is_sysmgr and not mp))
	parent["mobile_no"] = mobile

	anchor_vertical = mp.vertical if mp else item.get("custom_vertical")
	anchor_group = mp.crm_group if mp else item.get("custom_group")
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
		doc.save(ignore_permissions=True)
		return doc, "updated"

	parent.setdefault("first_name", "(no name)")
	parent.setdefault("status", "New")
	doc = frappe.new_doc("CRM Lead")
	doc.update(parent)
	_apply_children(doc, children)
	if mp:
		_force_routing(doc, mp)
	doc.insert(ignore_permissions=True)
	return doc, "created"


def _update_one(name, item, mp, is_sysmgr, parent_fields, child_allow):
	"""Update one lead by CRM name. Returns (doc, 'updated'). Scope-checked for partners."""
	if not name:
		frappe.throw(_("name (the CRM Lead id) is required for an update"))
	if not frappe.db.exists("CRM Lead", name):
		frappe.throw(_("Lead {0} not found").format(name), frappe.DoesNotExistError)
	doc = frappe.get_doc("CRM Lead", name)
	if mp and (doc.custom_vertical != mp.vertical or doc.custom_group != mp.crm_group):
		frappe.throw(_("Lead {0} is not in your scope").format(name), frappe.PermissionError)
	parent, children = _collect(item, parent_fields, child_allow, allow_routing=bool(is_sysmgr and not mp))
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
	if not frappe.db.exists("CRM Lead", name):
		frappe.throw(_("Lead {0} not found").format(name), frappe.DoesNotExistError)
	doc = frappe.get_doc("CRM Lead", name)
	if mp and (doc.custom_vertical != mp.vertical or doc.custom_group != mp.crm_group):
		frappe.throw(_("Lead {0} is not in your scope").format(name), frappe.PermissionError)
	frappe.delete_doc("CRM Lead", name, ignore_permissions=True)


def _read_list(data, key):
	"""Parse a request arg that should be a JSON list."""
	val = data.get(key)
	if val is None:
		return None
	if isinstance(val, str):
		val = frappe.parse_json(val)
	if not isinstance(val, list):
		val = [val]
	return val


def _run_bulk(items, fn):
	"""Run `fn(index, item)` per record in its own savepoint -> partial success.
	A failing record is rolled back and reported; the rest still commit."""
	if not isinstance(items, list):
		frappe.throw(_("Expected a JSON array"))
	if len(items) > BULK_MAX:
		frappe.throw(_("Max {0} records per call; received {1}. Page the rest.").format(BULK_MAX, len(items)))
	results, ok = [], 0
	for i, item in enumerate(items):
		sp = "tc_bulk_{0}".format(i)
		frappe.db.savepoint(sp)
		try:
			results.append(fn(i, item))
			ok += 1
		except Exception as e:
			frappe.db.rollback(save_point=sp)
			results.append({"index": i, "ok": False, "error": str(e)})
	return {"ok": True, "total": len(items), "succeeded": ok, "failed": len(items) - ok, "results": results}


# -- singular endpoints ------------------------------------------------------

@frappe.whitelist(methods=["GET"])
def lead_schema(**kwargs):
	"""Discovery: the fields THIS caller may send/read + their routing mode.
	Two partners hitting this get different field lists — driven by their grid."""
	user, mp, is_sysmgr, parent_fields, child_allow = _caller_fields()

	def describe(doctype, fields):
		m = frappe.get_meta(doctype)
		out = []
		for fn in fields:
			f = m.get_field(fn)
			if not f:
				continue
			out.append({
				"fieldname": fn, "label": f.label, "type": f.fieldtype,
				"required": bool(f.reqd),
				"options": (f.options or None) if f.fieldtype in ("Link", "Select") else None,
			})
		return out

	children = {}
	for section, cf in SECTION_CHILD.items():
		if child_allow[cf]:
			children[cf] = describe(SECTION_DOCTYPE[section], child_allow[cf])

	out = {
		"lead": describe("CRM Lead", parent_fields),
		"children": children,
		"child_write": "Send each child as a JSON array under its key, e.g. "
		               "custom_lab_profile=[{...}]. The existing row is merged field-by-field.",
		"dedup": "A lead is unique per (mobile_no, product line, group). Re-sending the same "
		         "trio updates that lead; a different line creates a new one.",
		"bulk": {"max_per_call": BULK_MAX, "list_page_max": LIST_MAX,
		         "list_filters": list(LIST_FILTERS.keys()) + ["mobile_no"]},
	}
	if mp:
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
	return out


@frappe.whitelist(methods=["GET"])
def lead_get(**kwargs):
	"""Read one lead by `name` or `mobile_no`. A partner only sees leads on their
	line, and only their allowed fields."""
	user, mp, is_sysmgr, parent_fields, child_allow = _caller_fields()
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
		frappe.throw(_("Lead not found in your scope"), frappe.DoesNotExistError)
	return _curate(frappe.get_doc("CRM Lead", lead_name), parent_fields, child_allow)


@frappe.whitelist(methods=["POST"])
def lead_create(**kwargs):
	"""Create-or-upsert a lead by phone. Returns the CRM `name` to PUT back to."""
	user, mp, is_sysmgr, parent_fields, child_allow = _caller_fields()
	doc, action = _upsert_one(frappe.form_dict, mp, is_sysmgr, parent_fields, child_allow)
	return _result(doc, action)


@frappe.whitelist(methods=["PUT"])
def lead_update(**kwargs):
	"""Update a lead by CRM `name`. Partner scope-checked; can't move it to another line."""
	user, mp, is_sysmgr, parent_fields, child_allow = _caller_fields()
	doc, action = _update_one(frappe.form_dict.get("name"), frappe.form_dict, mp, is_sysmgr, parent_fields, child_allow)
	return _result(doc, action)


@frappe.whitelist(methods=["DELETE"])
def lead_delete(**kwargs):
	"""Delete a lead by CRM `name`. Partner scope-checked (own line only). A lead with
	linked activity raises LinkExistsError — so a partner can't nuke a worked lead."""
	user, mp, is_sysmgr, parent_fields, child_allow = _caller_fields()
	name = frappe.form_dict.get("name")
	_delete_one(name, mp)
	return {"ok": True, "action": "deleted", "name": name}


# -- bulk / query endpoints --------------------------------------------------

@frappe.whitelist(methods=["POST"])
def lead_create_bulk(**kwargs):
	"""Create-or-upsert many leads. Body: {"leads":[{...}, ...]} (<= 100). Partial success."""
	user, mp, is_sysmgr, parent_fields, child_allow = _caller_fields()
	leads = _read_list(frappe.form_dict, "leads") or []

	def one(i, item):
		doc, action = _upsert_one(item, mp, is_sysmgr, parent_fields, child_allow)
		return {"index": i, "ok": True, "action": action, "name": doc.name}

	return _run_bulk(leads, one)


@frappe.whitelist(methods=["PUT"])
def lead_update_bulk(**kwargs):
	"""Update many leads. Body: {"updates":[{"name":..,..fields}, ...]} (<= 100). Partial success."""
	user, mp, is_sysmgr, parent_fields, child_allow = _caller_fields()
	updates = _read_list(frappe.form_dict, "updates") or []

	def one(i, item):
		doc, action = _update_one((item or {}).get("name"), item, mp, is_sysmgr, parent_fields, child_allow)
		return {"index": i, "ok": True, "action": action, "name": doc.name}

	return _run_bulk(updates, one)


@frappe.whitelist(methods=["DELETE"])
def lead_delete_bulk(**kwargs):
	"""Delete many leads. Body: {"names":[...]} (<= 100). Partial success."""
	user, mp, is_sysmgr, parent_fields, child_allow = _caller_fields()
	names = _read_list(frappe.form_dict, "names") or []

	def one(i, name):
		_delete_one(name, mp)
		return {"index": i, "ok": True, "action": "deleted", "name": name}

	return _run_bulk(names, one)


@frappe.whitelist(methods=["POST"])
def lead_get_bulk(**kwargs):
	"""Read many leads by `names` OR `mobile_nos` (<= 100). Out-of-scope ids omitted."""
	user, mp, is_sysmgr, parent_fields, child_allow = _caller_fields()
	data = frappe.form_dict
	names = _read_list(data, "names")
	mobiles = _read_list(data, "mobile_nos")
	if not names and not mobiles:
		frappe.throw(_("names or mobile_nos is required"))

	filters = {}
	if names:
		if len(names) > BULK_MAX:
			frappe.throw(_("Max {0} ids per call.").format(BULK_MAX))
		filters["name"] = ["in", names]
	else:
		if len(mobiles) > BULK_MAX:
			frappe.throw(_("Max {0} numbers per call.").format(BULK_MAX))
		filters["mobile_no"] = ["in", [_norm_phone(m) for m in mobiles]]
	if mp:
		filters["custom_vertical"] = mp.vertical
		filters["custom_group"] = mp.crm_group

	found = frappe.get_all("CRM Lead", filters=filters, pluck="name")
	leads = [_curate(frappe.get_doc("CRM Lead", n), parent_fields, child_allow) for n in found]
	return {"ok": True, "requested": len(names or mobiles), "found": len(leads), "leads": leads}


@frappe.whitelist(methods=["GET"])
def lead_list(**kwargs):
	"""List leads on the caller's line, filtered + paginated. Curated fields only, no
	children (use lead_get for the full record). Filters: status, created/updated date
	ranges, exact mobile_no — never arbitrary fields."""
	user, mp, is_sysmgr, parent_fields, child_allow = _caller_fields()
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

	limit = min(cint(data.get("limit")) or LIST_DEFAULT, LIST_MAX)
	offset = cint(data.get("offset") or data.get("limit_start"))

	fields = list(dict.fromkeys(
		parent_fields + ["name", "source", "custom_vertical", "custom_group", "custom_current_program"]
	))
	total = frappe.db.count("CRM Lead", filters)
	leads = frappe.get_all(
		"CRM Lead", filters=filters, fields=fields,
		limit_page_length=limit, limit_start=offset, order_by="modified desc",
	)
	return {"ok": True, "total": total, "count": len(leads), "offset": offset, "limit": limit, "leads": leads}
