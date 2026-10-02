"""Smart Views endpoints and their gates (catalog -> query -> api); the SPA and `exports._PRODUCERS` call these by dotted path, so none may move."""
import re

import frappe
import frappe.permissions
from frappe import _
from frappe.core.doctype.access_log.access_log import make_access_log
from frappe.query_builder.functions import Count
from frappe.utils import cint, cstr

from tatva_connect import exports, tab_order, tabular
from tatva_connect.access import entitlement, visibility
from tatva_connect.activity import api as activity_api
from tatva_connect.lead import filters as lead_filters
from tatva_connect.smartview import permissions as sv_perms
from tatva_connect.smartview.catalog import (
	LEAD_DOCTYPE,
	TASK_DOCTYPE,
	_always_shown,
	_catalog_fields,
	_col_type,
	_column_field_keys,
	_driving,
	_flat_label,
	_grains_for_view,
	_grains_from_axes,
	_is_identity,
	_saved_json,
	_search_keys,
	_settle_grain,
	_starter_columns,
	_validate_columns,
	_with_always_shown,
)
from tatva_connect.smartview.permissions import SMART_VIEW_DT
from tatva_connect.smartview.query import (
	_apply_filters,
	_apply_search,
	_filter_keys,
	_hydrate,
	_hydrate_split,
	_joins,
	_link_selectors,
	_link_titles,
	_predicate_keys,
	_predicate_where,
	_validate_predicate,
)
from tatva_connect.taxonomy import grain, labels

PAGE_MAX = 200
PAGE_DEFAULT = 50

# A stored column width (number + css unit), validated on the way in because the grid writes it straight into a style attribute.
_WIDTH = re.compile(r"^\d+(\.\d+)?(rem|px|em|ch|%)$")


@frappe.whitelist()
def field_catalog(base_object=None, activity_type=None, vertical=None, group=None, program=None, view=None):
	"""The picker's allowed fields: a saved `view` resolves exactly as `get_data` reads it; a draft scope (the editor) is gated as `upsert_view` gates it."""
	if view:
		v = frappe.get_doc(SMART_VIEW_DT, view)
		_assert_read(v)
		base_object, activity_type, grains = v.base_object, v.activity_type, _grains_for_view(v)
	else:
		if base_object not in ("Lead", "Activity"):
			frappe.throw(_("Unknown base object {0}").format(base_object))
		if base_object == "Activity" and activity_type:
			_assert_type_offered(activity_type, vertical, group, program)
		grains = _grains_from_axes(vertical, group, program)
	cat = _catalog_fields(base_object, activity_type, grains)
	out = []
	for r in cat.values():
		fieldtype, options = _col_type(r)
		out.append({
			"field_key": r.field_key,
			"label": _flat_label(r),
			"fieldname": r.fieldname,
			"sql_source": r.sql_source,
			"filterable": bool(r.filterable),
			"sortable": bool(r.sortable),
			"surface": r.surface or "worklist",
			"fieldtype": fieldtype,
			"options": options,
			# The scoped link query this column's FILTER control must use — the same one decision the native lenses relay, so both surfaces offer a composite master's label once.
			"link_query": labels.link_query(options) if fieldtype == "Link" else None,
			"section_title": r.get("section_title"),  # the editor groups its condition fields under it
		})
	# Columns the author may not remove, from the same declaration the composer projects by.
	always = set(_always_shown(base_object, cat))
	for row in out:
		row["always_shown"] = row["field_key"] in always
	# A view keys its rows by `field_key` (`lead:program`), so a grain axis is scoped by its `fieldname`.
	return lead_filters.stamp_grain_options(out, LEAD_DOCTYPE if base_object == "Lead" else TASK_DOCTYPE)


# Tabs — the read-only surface the SPA boots from; who may see a view is `permissions`' one predicate, so offer == open.

def _smart_view_tab(d, write=None, share=None):
	"""One tab row for the frontend store; `write`/`share` are `sv_perms.shared_names` for the sweep, None for one view."""
	return {
		"name": d.name,
		"label": d.label,
		"base_object": d.base_object,
		# The PK stays: the view filters on it. The label sits beside it, for display.
		"activity_type": d.activity_type,
		"activity_type_label": labels.label(d.activity_type, labels.TASK_TYPE),
		# The view's own grain, so the list's field pickers ask for the catalog this view resolves in.
		"vertical": d.vertical,
		"group": d.group,
		"program": d.program,
		"color": d.color,
		"icon": d.icon,
		"order": cint(d.view_order),
		"pinned": bool(d.pinned),
		# Presentation only — the grid applies it on its first paint so a remembered width never jumps.
		"column_widths": _saved_json(d, "column_widths", {}),
		"is_standard": bool(d.get("is_standard")),
		# The share dialog's fixed Owner row; frappe.share.get_users lists only the shares.
		"owner_user": d.get("owner_user"),
		"can_write": sv_perms.can_write(d, shared=write),
		"can_share": sv_perms.can_share(d, shared=share),
	}


@frappe.whitelist()
@frappe.read_only()
def get_smart_views():
	"""The caller's readable tabs (`permissions.can_read`), in their own dragged order; decides what is offered, never which rows are readable."""
	write, share = sv_perms.shared_names("write"), sv_perms.shared_names("share")
	return tab_order.apply([_smart_view_tab(r, write, share) for r in sv_perms.readable_views()], SMART_VIEW_DT)


def _assert_read(d):
	"""Fail-closed read gate — the ONE predicate, thrown the one way every endpoint throws it."""
	if not sv_perms.can_read(d):
		frappe.throw(_("Not permitted."), frappe.PermissionError)


def _assert_write(d):
	"""Fail-closed edit gate: the owner, an operator, or a write share."""
	if not sv_perms.can_write(d):
		frappe.throw(_("Not permitted."), frappe.PermissionError)


@frappe.whitelist()
@frappe.read_only()
def get_view(name):
	"""One view's full editable definition (scope, predicate, columns), behind the fail-closed read gate."""
	d = frappe.get_doc("CRM Smart View", name)
	_assert_read(d)
	return {
		"name": d.name,
		"label": d.label,
		"base_object": d.base_object,
		"activity_type": d.activity_type,
		"activity_type_label": labels.label(d.activity_type, labels.TASK_TYPE),
		"vertical": d.vertical,
		"group": d.group,
		"program": d.program,
		"description": d.description,
		"color": d.color,
		"icon": d.icon,
		"predicate": _saved_json(d, "predicate", None),
		"columns": _saved_json(d, "columns", []),
		"column_widths": _saved_json(d, "column_widths", {}),
		"is_standard": bool(d.is_standard),
		"owner_user": d.owner_user,
		"can_write": sv_perms.can_write(d),
		"can_share": sv_perms.can_share(d),
	}


@frappe.whitelist()
@frappe.read_only()
def get_data(view, filters=None, sort=None, search=None, columns=None, page=1, page_size=50, with_count=1,
             with_titles=1):
	"""The composer: {columns, rows, total} for a saved view, PQC-scoped; `columns` overrides transiently, `with_count=0` answers `total: None`."""
	v = frappe.get_doc("CRM Smart View", view)
	# A private view's column set is its definition, so it needs the read gate too.
	_assert_read(v)
	base_object = v.base_object
	activity_type = v.activity_type

	cat = _catalog_fields(base_object, activity_type, _grains_for_view(v))
	driving_name, driving_table = _driving(base_object)

	col_keys = _column_field_keys(v, cat)
	# The override wins over the saved set but is validated against the save path's allowlist, so an unknown key throws.
	if columns is not None:
		req = _validate_columns(columns, cat)
		if req:
			col_keys = _with_always_shown(req, base_object, cat)
	predicate = _saved_json(v, "predicate", None)
	if isinstance(filters, str):
		filters = (frappe.parse_json(filters) if str(filters).strip() else None) or []

	# Only join children referenced by columns OR predicate (lean).
	needed = set(col_keys)
	_predicate_keys(predicate, needed)
	_filter_keys(filters, needed)
	# The searched identity fields live on the driving row, so naming them here adds terms and no joins.
	search_keys = _search_keys(cat, col_keys, driving_name) if (search or "").strip() else set()
	needed |= search_keys

	def scoped(keys):
		"""(apply_joins, field_terms, compare_terms, criterion) for ONE join set; rebuilt per set because terms are bound to aliased tables."""
		apply_joins, field_terms, compare_terms = _joins(keys, cat, driving_table, driving_name)
		# WHERE: predicate tree + ad-hoc filters + search, ALL catalog-bounded.
		crit = _predicate_where(predicate, cat, compare_terms)
		crit = _apply_filters(crit, filters, cat, compare_terms)
		crit = _apply_search(crit, search, cat, field_terms, search_keys)
		# Activity view: pin the task type (indexed). Lead view: no extra base filter.
		if base_object == "Activity" and activity_type:
			tc = driving_table.custom_task_type == activity_type
			crit = tc if crit is None else (crit & tc)
		pqc = visibility.readable_criterion(driving_name, driving_table)
		if pqc is not None:
			crit = pqc if crit is None else (crit & pqc)
		return apply_joins, field_terms, compare_terms, crit

	# Every join yields one row per parent, so the count joins only what is filtered or searched, never what is merely projected.
	filtered_keys = set()
	_predicate_keys(predicate, filtered_keys)
	_filter_keys(filters, filtered_keys)

	# sort: [field_key, "asc"|"desc"], catalog-bounded; resolved before the join sets because an ORDER BY field must be joined.
	if isinstance(sort, str):
		sort = frappe.parse_json(sort)
	sort_key = sort[0] if (isinstance(sort, (list, tuple)) and sort) else None
	if sort_key and cat.get(sort_key) and cat[sort_key].sortable:
		needed.add(sort_key)  # ORDER BY reads a term, and `_joins` resolves one only for a key it is handed

	# A COUNT has no ORDER BY, so the sort column stays out of it.
	count_keys = filtered_keys | search_keys

	# Display-only off-row columns leave the query, since LIMIT applies after a join; every projected one is SHOWN through `_hydrate`, the one reader.
	must_query = filtered_keys | search_keys | ({sort_key} if sort_key else set())
	query_keys = needed - _hydrate_split(col_keys, must_query, cat)
	hydrate_keys = _hydrate_split(col_keys, set(), cat)

	# ---- count (PQC-scoped) -------------------------------------------------
	total = None
	if cint(with_count):
		count_joins, _cf, _cc, count_crit = scoped(count_keys)
		count_q = count_joins(frappe.qb.from_(driving_table).select(Count("*").as_("total")))
		if count_crit is not None:
			count_q = count_q.where(count_crit)
		total = cint(count_q.run(as_dict=True)[0].get("total"))

	# ---- rows ---------------------------------------------------------------
	apply_joins, field_terms, compare_terms, crit = scoped(query_keys)
	select_terms = [driving_table.name.as_("name")] + [
		field_terms[k].as_(k) for k in col_keys if k in field_terms and k not in hydrate_keys
	] + _link_selectors(col_keys, cat, driving_table)
	rows_q = apply_joins(frappe.qb.from_(driving_table).select(*select_terms))
	if crit is not None:
		rows_q = rows_q.where(crit)

	# Honoured or refused, never ignored (`query._apply_filters`): a silent `modified desc` is a lie.
	if sort_key and sort_key not in compare_terms:
		frappe.throw(_("{0} cannot be sorted on here.").format(sort_key))
	if sort_key:
		direction = frappe.qb.desc if (len(sort) > 1 and str(sort[1]).lower() == "desc") else frappe.qb.asc
		rows_q = rows_q.orderby(compare_terms[sort_key], order=direction)
	else:
		direction = frappe.qb.desc
		rows_q = rows_q.orderby(driving_table.modified, order=direction)
	# A unique last key for stable LIMIT/OFFSET pages, in the sort's own direction so an index ending in the sort column serves it.
	rows_q = rows_q.orderby(driving_table.name, order=direction)

	page = max(cint(page) or 1, 1)
	# Load More widens one window like the native `page_length`, bounded by the operator's export ceiling.
	size = min(max(cint(page_size), 0) or PAGE_DEFAULT, exports.row_cap())
	rows_q = rows_q.limit(size).offset((page - 1) * size)
	rows = rows_q.run(as_dict=True)

	_hydrate(rows, hydrate_keys, cat, driving_name)

	# ListView columns by `key`, with the plain label; `fieldname` drives native cell rendering and `options` names a Link's title target.
	columns = []
	for k in col_keys:
		if k not in field_terms and k not in hydrate_keys:
			continue
		# One type resolution per column.
		fieldtype, options = _col_type(cat[k])
		columns.append({"key": k, "label": cat[k].label or cat[k].fieldname, "fieldtype": fieldtype,
		                "options": options, "fieldname": cat[k].fieldname,
		                "identity": _is_identity(base_object, cat[k])})
	out = {"columns": columns, "rows": rows, "total": total}
	# ONE map for the page's Link columns; a download has no cells, so `with_titles=0` skips it.
	if cint(with_titles):
		titles = {}
		_link_titles(rows, col_keys, cat, titles)
		out["_link_titles"] = titles
	return out


# Authoring — the only write path: owner-scoped, catalog-validated, capped at OWNER_VIEW_CAP; the whitelisted method is the gate.

OWNER_VIEW_CAP = 20


def _assert_type_offered(activity_type, vertical, group, program):
	"""An Activity view's type must be one the editor's picker offers for the view's grain; the picker answers both, so they never disagree."""
	if activity_type not in {t["name"] for t in activity_api.list_types_for_grain(vertical, group, program)}:
		frappe.throw(_("This activity type is not offered for this grain."), frappe.PermissionError)


@frappe.whitelist()
def upsert_view(view):
	"""Create or update a view (dict or JSON) and return its tab shape; on update, resource, activity type and grain come off the stored row."""
	if isinstance(view, str):
		view = frappe.parse_json(view)
	if not isinstance(view, dict):
		frappe.throw(_("Invalid view payload."))

	user = frappe.session.user
	if user == "Guest":
		frappe.throw(_("Not permitted."), frappe.PermissionError)

	base_object = view.get("base_object")
	if base_object not in ("Lead", "Activity"):
		frappe.throw(_("Unknown base object {0}").format(base_object))
	label = (view.get("label") or "").strip()
	if not label:
		frappe.throw(_("A view name is required."))

	# Resource, activity type and grain are fixed at creation, so a partial payload can never blank an axis and widen the audience.
	name = view.get("name")
	if name:
		doc = frappe.get_doc("CRM Smart View", cstr(name))
		# The one write predicate: a view you don't own is operator-only to edit.
		if not sv_perms.can_write(doc):
			frappe.throw(_("You can only edit your own views."), frappe.PermissionError)
		base_object = doc.base_object
		activity_type = doc.activity_type
		vertical, group, program = doc.vertical, doc.group, doc.program
	else:
		activity_type = view.get("activity_type") or None
		# The chosen grain bounds the catalog columns and predicate are validated against.
		vertical, group, program = _settle_grain(
			view.get("vertical") or None, view.get("group") or None, view.get("program") or None
		)
		if base_object == "Activity":
			if not activity_type:
				frappe.throw(_("An activity type is required for an Activity view."))
			_assert_type_offered(activity_type, vertical, group, program)
		else:
			activity_type = None
		if frappe.db.count("CRM Smart View", {"owner_user": user, "is_standard": 0}) >= OWNER_VIEW_CAP:
			frappe.throw(_("You have reached the limit of {0} views.").format(OWNER_VIEW_CAP))
		doc = frappe.new_doc("CRM Smart View")
		doc.owner_user = user
	grains = _grains_from_axes(vertical, group, program)
	cat = _catalog_fields(base_object, activity_type, grains)
	# Materialised on save, so a view's projection is always an explicit stored list.
	columns = _validate_columns(view.get("columns"), cat) or _starter_columns(base_object, cat)
	predicate = view.get("predicate")
	if isinstance(predicate, str):
		predicate = frappe.parse_json(predicate) if predicate else None
	if predicate:
		_validate_predicate(predicate, cat)

	# `is_standard` belongs to `set_public` and ownership is set once at creation; neither is written here.
	doc.label = label
	doc.base_object = base_object
	doc.activity_type = activity_type
	doc.vertical = vertical
	doc.group = group
	doc.program = program
	doc.predicate = frappe.as_json(predicate) if predicate else None
	doc.columns = frappe.as_json(columns) if columns else None
	# An omitted field is left untouched; only a sent value, even blank, is written.
	if view.get("description") is not None:
		doc.description = view.get("description") or None
	if view.get("color") is not None:
		doc.color = view.get("color") or None
	if view.get("icon") is not None:
		doc.icon = view.get("icon") or None
	if view.get("pinned") is not None:
		doc.pinned = 1 if view.get("pinned") else 0
	if view.get("view_order") is not None:
		doc.view_order = cint(view.get("view_order"))
	doc.save(ignore_permissions=True)  # authz-ok: tier-a — smart-view scaffolding, operator-run
	return _smart_view_tab(doc)


@frappe.whitelist()
def set_column_widths(view, widths):
	"""Store dragged column widths without `modified` or validate; a caller who may not write gets `{"saved": False}`, not an error."""
	d = frappe.get_doc("CRM Smart View", view)
	if not sv_perms.can_write(d):
		return {"saved": False}
	widths = frappe.parse_json(widths) if isinstance(widths, str) and widths.strip() else (widths or {})
	if not isinstance(widths, dict):
		frappe.throw(_("Column widths must be an object of {field_key: width}."))
	# Only a plain CSS length for a column the grid shows, resolved off the same catalog `get_data` projects from.
	shown = _column_field_keys(d, _catalog_fields(d.base_object, d.activity_type, _grains_for_view(d)))
	clean = {
		k: v for k, v in widths.items()
		if k in shown and isinstance(v, str) and _WIDTH.match(v.strip())
	}
	frappe.db.set_value("CRM Smart View", view, "column_widths", frappe.as_json(clean),
	                    update_modified=False)
	return {"saved": True, "column_widths": clean}


# Sharing is frappe's (frappe.share.*), except the one thing a DocShare cannot say: everyone in the view's grain.
@frappe.whitelist()
def set_public(view, value):
	"""Offer the view to everyone entitled to its grain, or take that back; a share holder's call, and ownership never moves."""
	d = frappe.get_doc(SMART_VIEW_DT, view)
	if not sv_perms.can_share(d):
		frappe.throw(_("Not permitted."), frappe.PermissionError)
	public = bool(cint(value))
	frappe.db.set_value(SMART_VIEW_DT, view, "is_standard", 1 if public else 0)
	return {"is_standard": public}


@frappe.whitelist()
def share_user_query(doctype, txt, searchfield, start, page_len, filters):
	"""`search_link`'s query for the share picker: the people entitled to the view's grain."""
	filters = frappe.parse_json(filters) if isinstance(filters, str) else (filters or {})
	d = frappe.get_doc(SMART_VIEW_DT, filters.get("view"))
	if not sv_perms.can_share(d):
		frappe.throw(_("Not permitted."), frappe.PermissionError)
	return entitlement.link_rows(entitlement.users_entitled_to(
		tuple(d.get(axis) for axis in grain.AXES), txt=txt, limit=cint(page_len) or 20))


# Export — the screen as a file, never a second query.
def _assert_may_export(view):
	"""Read gate + native export permission, returning `(view doc, driving doctype)`; asked in the request and again in the worker."""
	d = frappe.get_doc(SMART_VIEW_DT, view)
	_assert_read(d)
	driving_name, _tbl = _driving(d.base_object)
	if not frappe.permissions.can_export(driving_name):
		frappe.throw(
			_("You do not have permission to export {0}.").format(driving_name), frappe.PermissionError
		)
	return d, driving_name


@frappe.whitelist()
def export_view(view, fmt="csv", filters=None, search=None, sort=None, columns=None, limit=None):
	"""Gate, then queue this view as a file job that re-runs `get_data` in a worker; `limit` can only narrow below `row_cap()`."""
	_assert_may_export(view)  # a gate, not a read: the queue path below asks it again for what it returns
	fmt = (fmt or "csv").lower()
	if fmt not in tabular.FORMATS:
		frappe.throw(_("Unsupported export format {0}.").format(fmt))

	return exports.queue("Smart View", view, fmt,
	                     {"filters": filters, "search": search, "sort": sort, "columns": columns,
	                      "limit": limit})


# The OUTERMOST read of the drain: the pages below ride this one switch, so the file is one snapshot.
@frappe.read_only()
def produce_export(job, params, progress):
	"""The Smart View producer for `exports`: re-gates, pages `get_data` in PAGE_MAX windows up to `exports.row_cap()`."""
	d, driving_name = _assert_may_export(job.reference)

	# A limit can only narrow the operator's ceiling; no limit means the ceiling alone.
	asked_for = frappe.cint(params.get("limit")) or None
	cap = min(exports.row_cap(), asked_for) if asked_for else exports.row_cap()
	cols, rows = [], []
	# One label read per distinct value per column.
	seen = {}
	page = 1
	while len(rows) < cap:
		# No count, no titles: a wider window cannot change how many rows MATCHED, and a file has no cells to title.
		data = get_data(job.reference, filters=params.get("filters"), sort=params.get("sort"),
		                search=params.get("search"), columns=params.get("columns"),
		                page=page, page_size=PAGE_MAX, with_count=0, with_titles=0)
		cols = cols or data["columns"]
		batch = data["rows"]
		if not batch:
			break
		rows.extend([_export_cell(c, r.get(c["key"]), seen) for c in cols] for r in batch)
		progress(len(rows))
		if len(batch) < PAGE_MAX:
			break
		page += 1
	# Only the CEILING truncates; a reader who got the rows they asked for was not cut short.
	truncated = len(rows) >= cap and (asked_for is None or cap < asked_for)
	rows = rows[:cap]

	# Logged where the file becomes REAL: a queued export that produced nothing is not a read that left.
	make_access_log(doctype=driving_name, file_type=job.fmt.upper(), report_name=d.label,
	                filters=frappe.as_json({"smart_view": job.reference, "filters": params.get("filters"),
	                                        "search": params.get("search"), "rows": len(rows),
	                                        "truncated": truncated}),
	                columns=frappe.as_json([c["key"] for c in cols]))
	return {
		"stem": d.label or "smart-view",
		"ext": job.fmt,
		"content": tabular.write([c["label"] for c in cols], rows, job.fmt),
		"rows": len(rows),
		"truncated": truncated,
	}


def _export_cell(column, value, seen):
	"""A cell as the grid reads it: a composite Link key via `labels.shown_at`, `None` as empty; the formula guard lives in `tabular.write`."""
	if value is None:
		return ""
	target = column.get("options") if column.get("fieldtype") == "Link" else None
	if not (target and labels.is_composite(target)):
		return value
	key = (column["key"], value)
	if key not in seen:
		seen[key] = labels.shown_at(target, value)
	return seen[key]


@frappe.whitelist()
def can_export(base_object):
	"""Whether to offer the download, on `frappe.permissions.can_export`, the check list exports use too."""
	driving_name, _tbl = _driving(base_object)
	return bool(frappe.permissions.can_export(driving_name))


@frappe.whitelist()
def delete_view(name):
	"""Delete a Smart View: the owner or an operator only, since frappe never lets a share grant delete."""
	doc = frappe.get_doc("CRM Smart View", name)
	if not sv_perms.owns(doc):
		frappe.throw(_("You can only delete your own views."), frappe.PermissionError)
	frappe.delete_doc("CRM Smart View", name, ignore_permissions=True)  # authz-ok: tier-a — smart-view scaffolding, operator-run
	return {"deleted": name}
