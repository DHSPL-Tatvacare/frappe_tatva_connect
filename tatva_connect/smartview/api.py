"""Smart Views — THE DOORS. Every whitelisted endpoint the surface has, and the gates on them.

The third of the composer's three concerns (`catalog` -> `query` -> `api`). What a field IS lives in
`catalog`; how it becomes SQL lives in `query`; this module owns the request: who may ask, what a saved
view is allowed to say, and the shape of the answer. `permissions` owns who may read or write one.

A Smart View is a saved tabbed list over leads (one row per patient) or activities (one row per CRM Task
of an activity type). `get_data` is the composer proper, and it does six things in order:

  1. drives off CRM Lead (lead view) or CRM Task WHERE custom_task_type = activity_type,
  2. joins only what the columns or the predicate reference (`query._joins`),
  3. ANDs the permission query conditions — ALWAYS, fail-closed, on the rows AND the count,
  4. translates the saved predicate tree into nested qb WHERE (catalog fields only),
  5. applies ad-hoc filters, search and sort (catalog-bounded) and paginates on a unique last key,
  6. returns {columns, rows, total} — total being the tab's PQC-scoped count.

THE DOTTED PATHS ARE THE CONTRACT. The SPA calls these by name as strings, and `exports._PRODUCERS`
holds `tatva_connect.smartview.api.produce_export`. Every endpoint stays in this module for that reason;
moving one is a breaking change no test would catch.
"""
import re

import frappe
from frappe import _
from frappe.core.doctype.access_log.access_log import make_access_log
from frappe.query_builder.functions import Count
from frappe.utils import cint, cstr

from tatva_connect import exports, tab_order, tabular
from tatva_connect.access import entitlement, visibility
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
	_identity_key,
	_lead_catalog,
	_search_keys,
	_settle_grain,
	_starter_columns,
	_validate_columns,
	_with_always_shown,
)
from tatva_connect.smartview.query import (
	_apply_filters,
	_apply_search,
	_hydrate,
	_hydrate_split,
	_joins,
	_lead_titles,
	_link_titles,
	_predicate_keys,
	_predicate_where,
	_validate_predicate,
)
from tatva_connect.taxonomy import labels

SMART_VIEW_DT = "CRM Smart View"
PAGE_MAX = 200
PAGE_DEFAULT = 50

# A stored column width, and the only shape one may take: digits, an optional decimal, then a css unit.
# It is written straight into a style attribute by the grid, so it is validated on the way IN.
_WIDTH = re.compile(r"^\d+(\.\d+)?(rem|px|em|ch|%)$")


@frappe.whitelist()
def field_catalog(base_object, activity_type=None, vertical=None, group=None, program=None):
	"""The allowed fields for the picker/condition builder — type + grain scoped, role-restricted.
	The single source the editor (P2) and the composer agree on. The editor passes its selected
	grain (vertical/group/program); with none chosen the caller's entitled grains apply."""
	if base_object not in ("Lead", "Activity"):
		frappe.throw(_("Unknown base object {0}").format(base_object))
	grains = _grains_from_axes(vertical, group, program)
	cat = _catalog_fields(base_object, activity_type, grains, frappe.get_roles())
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
		})
	# Which of them the author may not remove, from the SAME declaration the composer projects by — the
	# picker cannot offer to drop a column the read path puts back. "Always shown", never "pinned": this
	# app spends that word on a pinned tab, and a frozen column is something frappe-ui cannot do at all.
	always = set(_always_shown(base_object, cat))
	for row in out:
		row["always_shown"] = row["field_key"] in always
	# A view keys its rows by `field_key` (`lead:program`), so a grain axis is scoped by its `fieldname`.
	return lead_filters.stamp_grain_options(out, LEAD_DOCTYPE if base_object == "Lead" else TASK_DOCTYPE)


# ---------------------------------------------------------------------------
# Tabs — the READ-ONLY surface the SPA boots from. get_smart_views feeds the store
# (the row of tabs); read-only, returns [] (never throws) when nothing is offered.
# Who may see a view is smartview/permissions.py's ONE predicate — offer == open.
# ---------------------------------------------------------------------------

def _smart_view_tab(d):
	"""One tab row for the frontend store — the minimal shape SmartViewTabs renders."""
	return {
		"name": d.name,
		"label": d.label,
		"base_object": d.base_object,
		# The PK stays: the view filters on it. The label sits beside it, for display.
		"activity_type": d.activity_type,
		"activity_type_label": labels.label(d.activity_type, labels.TASK_TYPE),
		"color": d.color,
		"icon": d.icon,
		"order": cint(d.view_order),
		"pinned": bool(d.pinned),
		# Presentation only — the grid applies it on its first paint so a remembered width never jumps.
		"column_widths": frappe.parse_json(d.column_widths) if d.get("column_widths") else {},
		"is_standard": bool(d.get("is_standard")),
		"can_write": sv_perms.can_write(d),
	}


@frappe.whitelist()
@frappe.read_only()
def get_smart_views():
	"""The caller's tabs: the standard views whose RULE grain overlaps their entitlement, plus their
	own, plus any shared with them (native DocShare). Ordered. Read-only; returns [] (never throws)
	when nothing is offered, so the surface degrades gracefully.

	ONE predicate decides this — `smartview/permissions.can_read`, the same answer `get_view`,
	`get_data` and `export_view` enforce — so a tab that is offered always opens. The rows inside any
	view are still the viewer's own (the composer ANDs their permission conditions on every run):
	this decides what is OFFERED, never what rows are readable.

	ARRANGED BY THE READER, LAST. The server's own order (`view_order asc, label asc`) is the starting
	point; `tab_order.apply` then lifts whatever this person dragged into place. It is a hint and never a
	filter — a view they have not arranged sorts after the ones they have, and a name in a stale
	arrangement is ignored — so a view created, shared or unshared since can never go missing here."""
	return tab_order.apply([_smart_view_tab(r) for r in sv_perms.readable_views()], SMART_VIEW_DT)


def _assert_read(d):
	"""Fail-closed read gate — the ONE predicate, thrown the one way every endpoint throws it."""
	if not sv_perms.can_read(d):
		frappe.throw(_("Not permitted."), frappe.PermissionError)


def _assert_write(d):
	"""The same, for the write half — share, unshare, publish and the recipient list they share a gate with.
	`upsert_view` and `delete_view` ask the SAME predicate but say what the caller was trying to do, which
	is worth more to them than one sentence for six acts."""
	if not sv_perms.can_write(d):
		frappe.throw(_("Not permitted."), frappe.PermissionError)


@frappe.whitelist()
@frappe.read_only()
def get_view(name):
	"""The full editable definition of one view (for the authoring editor): scope + parsed
	predicate + column keys. Gated by the one predicate (standard-in-grain / own / shared /
	operator); otherwise refused (fail-closed). Read-only."""
	d = frappe.get_doc("CRM Smart View", name)
	_assert_read(d)
	try:
		predicate = frappe.parse_json(d.predicate) if d.predicate else None
	except Exception:
		frappe.write_only()(frappe.log_error)(title="smartview: corrupt predicate JSON", message=f"view={d.name}")
		predicate = None
	try:
		columns = frappe.parse_json(d.columns) if d.columns else []
	except Exception:
		frappe.write_only()(frappe.log_error)(title="smartview: corrupt columns JSON", message=f"view={d.name}")
		columns = []
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
		"predicate": predicate,
		"columns": columns,
		"column_widths": frappe.parse_json(d.column_widths) if d.column_widths else {},
		"is_standard": bool(d.is_standard),
		"can_write": sv_perms.can_write(d),
	}


@frappe.whitelist()
@frappe.read_only()
def get_data(view, filters=None, sort=None, search=None, columns=None, page=1, page_size=50, with_count=1,
             with_titles=1):
	"""THE composer. Returns {columns, rows, total} for a saved CRM Smart View, PQC-scoped.
	Read-only. One qb query for the rows + one for the count; both AND the PQC.

	`columns` (optional) is an interactive, catalog-bounded override of the saved column set
	(the ColumnSettings picker) — a transient projection, never persisted by this read path.

	`with_count=0` skips the COUNT entirely and returns `total: None`. Widening the page window cannot
	change how many rows MATCHED, so Load More asks the same question a second time for nothing — and that
	count is the unbounded half of this call, while the rows are capped at PAGE_MAX."""
	v = frappe.get_doc("CRM Smart View", view)
	# The same gate every other read wears (SV-02): rows were always PQC-scoped, but a private view's
	# COLUMN SET is its definition, and it used to come back to any authenticated caller who knew the name.
	_assert_read(v)
	base_object = v.base_object
	activity_type = v.activity_type

	cat = _catalog_fields(base_object, activity_type, _grains_for_view(v), frappe.get_roles())
	driving_name, driving_table = _driving(base_object)

	col_keys = _column_field_keys(v, cat)
	# The interactive override wins over the saved set but stays catalog-bounded: a requested projection is
	# validated against the SAME allowlist the save path uses, so an unknown key throws instead of being
	# dropped and a wrong client key surfaces on the first click rather than months later.
	if columns is not None:
		req = _validate_columns(columns, cat)
		if req:
			col_keys = _with_always_shown(req, base_object, cat)
	try:
		predicate = frappe.parse_json(v.predicate) if v.predicate else None
	except Exception:
		frappe.write_only()(frappe.log_error)(title="smartview: bad predicate JSON")
		predicate = None
	if isinstance(filters, str):
		filters = (frappe.parse_json(filters) if str(filters).strip() else None) or []

	# Only join children referenced by columns OR predicate (lean).
	needed = set(col_keys)
	_predicate_keys(predicate, needed)
	for f in filters or []:
		if isinstance(f, (list, tuple)) and len(f) == 3:
			needed.add(f[0])
	# The searched identity fields live on the driving row, so naming them here adds terms and no joins.
	search_keys = _search_keys(cat, col_keys, driving_name) if (search or "").strip() else set()
	needed |= search_keys

	def scoped(keys):
		"""(apply_joins, field_terms, compare_terms, criterion) for ONE join set. The WHERE is rebuilt from that set's own
		terms because a term is a Field bound to an aliased table — a criterion built over one join set
		cannot be re-used over another, and an answer alias is positional. Same three functions, same
		catalog, asked once per set: no second query builder and no second filter rule."""
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

	# THE COUNT DOES NOT PAY FOR PROJECTION. Every join this composer builds is a `ROW_NUMBER() … = 1`
	# sub-select, so it yields exactly ONE row per parent and cannot change how many parents match — a
	# join that exists only because a COLUMN projects it is pure cost in the count. Dropping those turned
	# a wide activity worklist from one full scan of the answer table per displayed column into one per
	# FILTERED column, on every page load. `test_the_count_does_not_pay_for_projection` locks the number.
	#
	# Search remains the exception: `_search_keys` covers what the view PROJECTS as well as the row's own
	# identity, so when a term is typed those joins genuinely sit in the WHERE and the count needs them.
	filtered_keys = set()
	_predicate_keys(predicate, filtered_keys)
	for f in filters or []:
		if isinstance(f, (list, tuple)) and len(f) == 3:
			filtered_keys.add(f[0])

	# sort: [field_key, "asc"|"desc"], sortable + catalog bounded; default modified desc. Resolved HERE,
	# before the join sets are chosen, because a field the query ORDERS BY has to be in the query.
	if isinstance(sort, str):
		sort = frappe.parse_json(sort)
	sort_key = sort[0] if (isinstance(sort, (list, tuple)) and sort) else None
	if sort_key and cat.get(sort_key) and cat[sort_key].sortable:
		filtered_keys.add(sort_key)
	else:
		sort_key = None

	# A COUNT has no ORDER BY, so the column the page is SORTED by is pure cost in it — and a sort on a
	# child or answer column drags a whole windowed sub-select in on every page load. The rows query still
	# needs it (`must_query` below): you cannot order a page by a column that is not in the query.
	count_keys = (filtered_keys - {sort_key}) | search_keys if sort_key else filtered_keys | search_keys

	# THE PAGE IS FETCHED, THEN FILLED IN. A value that lives off the driving row costs a join, and the
	# page's LIMIT is applied AFTER that join — so the join walks the whole table to return fifty rows and
	# its cost grows with the data, never with the page. Measured on UAT (117,529 tasks, 56,745 child
	# rows): one task column 0.6s; the same page plus ONE child column 30s at 100% CPU, and MariaDB
	# refusing it as MAX_JOIN_SIZE. Resolving the page first and reading the child for those fifty
	# parents: instant.
	#
	# So a column the view only DISPLAYS leaves the query entirely and is read afterwards for the page's
	# own rows (`_hydrate`). A column that is FILTERED, SORTED or SEARCHED on stays in the query — you
	# cannot page a list before you have narrowed it. Displayed-only is the common case and the expensive
	# one.
	must_query = filtered_keys | search_keys
	hydrate_keys = _hydrate_split(col_keys, must_query, cat)
	query_keys = needed - hydrate_keys

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
	select_terms = [driving_table.name.as_("name")] + [field_terms[k].as_(k) for k in col_keys if k in field_terms]
	rows_q = apply_joins(frappe.qb.from_(driving_table).select(*select_terms))
	if crit is not None:
		rows_q = rows_q.where(crit)

	if sort_key and sort_key in compare_terms:
		direction = frappe.qb.desc if (len(sort) > 1 and str(sort[1]).lower() == "desc") else frappe.qb.asc
		rows_q = rows_q.orderby(compare_terms[sort_key], order=direction)
	else:
		rows_q = rows_q.orderby(driving_table.modified, order=frappe.qb.desc)
	# A UNIQUE last key, always. `modified` is not unique and neither is any column a rep may sort by, so
	# rows that tie have no defined order between one page and the next — and an export walks up to a
	# hundred thousand of them in LIMIT/OFFSET windows against a table people are still editing. Without
	# this a tied row can be read on two pages, or on none, and the file is quietly wrong either way.
	rows_q = rows_q.orderby(driving_table.name)

	page = max(cint(page) or 1, 1)
	size = min(cint(page_size) or PAGE_DEFAULT, PAGE_MAX)
	rows_q = rows_q.limit(size).offset((page - 1) * size)
	rows = rows_q.run(as_dict=True)

	_hydrate(rows, hydrate_keys, cat, driving_name)
	identity_key = _identity_key(base_object, col_keys, cat)

	# The plain label: the section prefix is for the PICKER, and in the grid the column is already in context.
	# `fieldname` rides along because a standard column is rendered by its framework NAME, not by its type:
	# the native list draws `_assign` as avatars off exactly that literal (`Leads.vue:184`). `options` rides
	# along for the same reason — a Link cell resolves its title out of the `_link_titles` map, which is
	# keyed `{target}::{value}`, so the cell has to be told which target this column points at.
	# `key`, not `field_key`: this list feeds frappe-ui's ListView, whose columns are addressed by `key`
	# (`ListRow.vue:48`). The catalog answers with `field_key` because that is the column's name in its own
	# doctype. Each name belongs to its own consumer.
	columns = []
	for k in col_keys:
		if k not in field_terms and k not in hydrate_keys:
			continue
		# ONE resolution per column, unpacked — the shape `field_catalog` uses. Asked twice, it walked the
		# meta and the std-field list a second time for the half of the answer it had just thrown away.
		fieldtype, options = _col_type(cat[k])
		columns.append({"key": k, "label": cat[k].label or cat[k].fieldname, "fieldtype": fieldtype,
		                "options": options, "fieldname": cat[k].fieldname, "identity": k == identity_key})
	# The response names its own page, so a reader accumulating pages cannot file a cached one as the first.
	out = {"columns": columns, "rows": rows, "total": total, "page": page}
	# A LEAD view's row IS the lead and its values are live, so the identity cell may be the same chip the
	# native lists draw. An ACTIVITY view's name column is a snapshot of what it was at the punch (D-C), so
	# it must keep showing that and never today's title.
	# A download has no cells, so `with_titles=0` skips the map entirely rather than paying for a chip nobody draws.
	# ONE map for the whole page: the identity column's leads (a Lead row IS the lead, so its own name is
	# the value) plus every Link column's target. A download has no cells, so `with_titles=0` skips it.
	if cint(with_titles):
		titles = _lead_titles(r.get("name") for r in rows) if base_object == "Lead" else {}
		_link_titles(rows, col_keys, cat, titles)
		out["_link_titles"] = titles
	return out


# ---------------------------------------------------------------------------
# Authoring (P2) — the ONLY write path. Owner-scoped + catalog-validated.
# Every field_key in a predicate or column set must be a catalog row for the view's
# scope (fail-closed allowlist); standard (grain-shared) views are operator-only;
# a non-operator owns at most OWNER_VIEW_CAP personal views. Writes run our own
# validation then save with ignore_permissions — the whitelisted method IS the gate
# (invariant: server-scoped writes), so the doctype stays System-Manager-only in Desk.
# ---------------------------------------------------------------------------

OWNER_VIEW_CAP = 20


def _assert_type_entitled(activity_type):
	"""An Activity view may only be AUTHORED on a task type the caller's grain reaches.

	Clamped at authoring and not at read, which is exactly how a Lead view is treated: `_grains_from_axes`
	refuses an out-of-grain axis on the way in, while a reader of a view shared across business lines is
	deliberately never re-clamped (`smartview/permissions.py` — a share is not grain-filtered, or handing
	one on would silently do nothing). One resource, one rule; gating the read here instead would break
	every cross-line shared Activity view.

	The type's axes are read off its own row, never split out of its composite `::` name — the separator
	is an autoname format the master owns, not a convention this file may assume."""
	axes = frappe.db.get_value("CRM Task Type", activity_type, ["vertical", "group", "program"], as_dict=True)
	if not axes:
		frappe.throw(_("Unknown activity type {0}").format(activity_type))
	# `grain_overlaps_entitlement`, not `grain_entitled`: a type's grain is a CONTRACT grain, free to leave
	# an axis blank meaning ANY, and `grain_entitled` takes a real record's DATA grain — its own docstring
	# says handing it a wildcard "would compare that wildcard as the empty string and answer confidently
	# wrong". It did: a type keyed `Goodflip-Care::Anaya::` (blank program) was refused to a user entitled
	# within that group, after the picker had offered it. Same predicate `activity.api.user_query` already
	# asks of these exact axes, and `smartview/permissions` asks of a saved view's own grain.
	if not entitlement.grain_overlaps_entitlement((axes.vertical or "", axes.group or "", axes.program or "")):
		frappe.throw(_("You are not entitled to this activity type."), frappe.PermissionError)


@frappe.whitelist()
def upsert_view(view):
	"""Create or update a Smart View — owner-scoped, catalog-validated. `view` is a dict (or
	JSON string): {name?, label, base_object, activity_type?, predicate?, columns?, description?,
	color?, icon?, view_order?, pinned?}. Returns the saved tab shape.

	`is_standard` is not part of this payload — `set_public` owns it. On an UPDATE the resource, activity
	type and grain come off the stored row, not the payload: they are what the view IS, fixed when it was
	created, and the editor disables all three on edit."""
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

	# WHAT A VIEW *IS* — its resource, its activity type and its grain — is fixed when it is created, and
	# an update reads all three off the stored row. The editor disables those three controls on edit and
	# says so; the server said nothing, so an update that simply omitted an axis (a partial payload, a
	# client that drops blanks) blanked it — and a view declaring no axis is site-wide, offered to
	# everyone. A save must not be able to widen a view's audience by leaving a field out.
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
		if base_object == "Activity":
			if not activity_type:
				frappe.throw(_("An activity type is required for an Activity view."))
			_assert_type_entitled(activity_type)
		else:
			activity_type = None
		# The view's chosen grain bounds its catalog: columns/predicate are validated against exactly
		# the fields visible in that grain, so a saved view can never carry an out-of-grain field.
		vertical, group, program = _settle_grain(
			view.get("vertical") or None, view.get("group") or None, view.get("program") or None
		)
		if frappe.db.count("CRM Smart View", {"owner_user": user, "is_standard": 0}) >= OWNER_VIEW_CAP:
			frappe.throw(_("You have reached the limit of {0} views.").format(OWNER_VIEW_CAP))
		doc = frappe.new_doc("CRM Smart View")
		doc.owner_user = user
	grains = _grains_from_axes(vertical, group, program)
	cat = _catalog_fields(base_object, activity_type, grains, frappe.get_roles())
	# Materialised on save, so a view's projection is always an explicit stored list. What "empty" meant
	# then stops depending on what the catalog happens to hold later.
	columns = _validate_columns(view.get("columns"), cat) or _starter_columns(cat)
	predicate = view.get("predicate")
	if isinstance(predicate, str):
		predicate = frappe.parse_json(predicate) if predicate else None
	if predicate:
		_validate_predicate(predicate, cat)

	# `is_standard` is NOT written here. Publishing a view is `set_public`'s one job, the way a dragged
	# column width is `set_column_widths`' — this endpoint used to hold a second, stricter rule for the
	# same field (operator-only), so the same act was allowed at one door and refused at the other. One
	# field, one door. Ownership is likewise set once, at creation: reassigning it on every save handed
	# a view to whoever last edited it.
	doc.label = label
	doc.base_object = base_object
	doc.activity_type = activity_type
	doc.vertical = vertical
	doc.group = group
	doc.program = program
	doc.predicate = frappe.as_json(predicate) if predicate else None
	doc.columns = frappe.as_json(columns) if columns else None
	doc.description = view.get("description") or None
	doc.color = view.get("color") or None
	doc.icon = view.get("icon") or None
	if view.get("pinned") is not None:
		doc.pinned = 1 if view.get("pinned") else 0
	if view.get("view_order") is not None:
		doc.view_order = cint(view.get("view_order"))
	doc.save(ignore_permissions=True)  # authz-ok: tier-a — smart-view scaffolding, operator-run
	return _smart_view_tab(doc)


@frappe.whitelist()
def set_column_widths(view, widths):
	"""Remember what a rep dragged the grid to. Presentation only — it touches no query.

	Its own endpoint rather than a field on `upsert_view`, because dragging a column is not authoring a
	view: it must not re-validate a predicate, must not re-check a column set, and must not fail because
	the saved definition has drifted. It writes ONE field.

	Gated by the SAME rule the rest of the write path uses (`permissions.can_write`) — a standard view is
	operator-only, a personal view is its owner's — and a caller who may not write simply keeps the width
	for their session rather than being shown an error for dragging a column.

	`db.set_value`, not `save()`: this is a presentation preference, so it must not bump `modified` (which
	would make every drag look like an edit in the audit trail) and must not fire the doctype's validate.
	"""
	d = frappe.get_doc("CRM Smart View", view)
	if not sv_perms.can_write(d):
		return {"saved": False}
	widths = frappe.parse_json(widths) if isinstance(widths, str) and widths.strip() else (widths or {})
	if not isinstance(widths, dict):
		frappe.throw(_("Column widths must be an object of {field_key: width}."))
	# Bounded and sanitised: only keys this view actually projects, and only a plain CSS length. A width
	# is echoed back into a style attribute, so nothing else is allowed to survive the round trip.
	# What the GRID shows, not what the author picked: the always-shown identity columns lead every projection
	# (`_with_always_shown`), so validating against the raw saved list silently discarded the width of the very
	# first column on the page — and still answered `{"saved": True}`. Resolved against the BASE catalog,
	# never the caller's scoped one: a width is presentation, so what it needs is the app's declaration, not
	# this caller's entitlement — and dragging a column must not pay for a grain and role resolution.
	base_cat = _lead_catalog() if d.base_object == "Lead" else {}
	saved = (frappe.parse_json(d.columns) if d.columns else []) + list(_always_shown(d.base_object, base_cat))
	clean = {
		k: v for k, v in widths.items()
		if k in saved and isinstance(v, str) and _WIDTH.match(v.strip())
	}
	frappe.db.set_value("CRM Smart View", view, "column_widths", frappe.as_json(clean),
	                    update_modified=False)
	return {"saved": True, "column_widths": clean}


# ---------------------------------------------------------------------------
# Sharing — frappe's OWN DocShare. A view is a saved QUESTION, never a saved answer: sharing one grants
# no data. Every run still ANDs the VIEWER's permission conditions, so two people opening one shared view
# see different rows. That is what makes this safe to hand out.
# ---------------------------------------------------------------------------
@frappe.whitelist()
def share_view(view, user):
	"""Share a view with one user, through frappe's own DocShare writer.

	Gated by the SAME rule as every other write here: you may share a view you may edit. The doctype's
	DocPerms stay System-Manager-only, so `check_share_permission` would refuse the very OWNER this
	endpoint exists for (frappe/share.py:56) — the flag skips frappe's gate because OURS already ran.
	`add_docshare` still does the rest: the row, the de-duplication, the notification.

	A share GRANTS ACCESS and nothing finer. It carried a `write` argument that no caller ever sent and
	that `sv_perms.can_write` does not read, so a write-share granted exactly what a read-share did."""
	d = frappe.get_doc(SMART_VIEW_DT, view)
	_assert_write(d)
	if not frappe.db.exists("User", user):
		frappe.throw(_("{0} is not a user.").format(user))
	# authz-ok: tier-b — gated by sv_perms.can_write above; DocPerms are deliberately SM-only
	frappe.share.add_docshare(SMART_VIEW_DT, view, user, read=1, notify=1,
	                          flags={"ignore_share_permission": True})
	return shared_with(view)


@frappe.whitelist()
def unshare_view(view, user):
	"""Take a share back, through frappe's own unshare — `remove()` refuses the owner (see share_view)."""
	d = frappe.get_doc(SMART_VIEW_DT, view)
	_assert_write(d)
	frappe.share.set_docshare_permission(SMART_VIEW_DT, view, user, "read", value=0,
	                                     flags={"ignore_share_permission": True})
	return shared_with(view)


@frappe.whitelist()
def shared_with(view):
	"""Who this view is shared with — on the SAME write gate as share, unshare and publish.

	Being able to OPEN a view is not being able to see who else was handed it: a standard view is offered
	to a whole grain, so a read gate made every recipient list on it readable by everyone in that grain.
	This is only ever drawn inside the share dialog, which is a write act."""
	d = frappe.get_doc(SMART_VIEW_DT, view)
	_assert_write(d)
	return frappe.get_all(  # authz-ok: tier-b — gated by sv_perms.can_write on the view these shares belong to
		"DocShare",
		filters={"share_doctype": SMART_VIEW_DT, "share_name": view},
		fields=["user"],  # a share grants access; there is no finer level to report
	)


@frappe.whitelist()
def set_public(view, value):
	"""Offer a view to everyone entitled to its grain, or take it back. Kept as its own endpoint for the
	same reason crm keeps one: it is not authoring a view.

	ON THE ONE WRITE GATE, like share and delete — you may publish a view you may edit. It was
	operator-only, which is a second rule for the same act, and the dialog drew the switch for anyone a
	role called a manager, so people were offered a control the server then refused.

	OWNERSHIP SURVIVES PUBLISHING. Clearing `owner_user` (crm's "a public view belongs to nobody") is what
	would make this a one-way door for its author: disowned, they no longer pass `can_write`, so they could
	never take it back. Who may OPEN a standard view is `is_standard` plus the grain rule, and that is
	untouched by keeping the author's name on it."""
	d = frappe.get_doc(SMART_VIEW_DT, view)
	_assert_write(d)
	public = bool(cint(value))
	frappe.db.set_value(SMART_VIEW_DT, view, {
		"is_standard": 1 if public else 0,
		"owner_user": d.owner_user or frappe.session.user,
	})
	return {"is_standard": public}


# ---------------------------------------------------------------------------
# Export — the SCREEN, as a file. Never a second query.
# ---------------------------------------------------------------------------
def _assert_may_export(view):
	"""The two gates a download passes, and the pair every caller needs after them: `(view doc, driving
	doctype)`. Asked in the REQUEST so a refusal is an error on the click, and again in the WORKER because
	a job row outlives the request that made it and entitlement can move in between."""
	d = frappe.get_doc(SMART_VIEW_DT, view)
	_assert_read(d)
	driving_name, _tbl = _driving(d.base_object)
	if not frappe.has_permission(driving_name, "export"):
		frappe.throw(
			_("You do not have permission to export {0}.").format(driving_name), frappe.PermissionError
		)
	return d, driving_name


@frappe.whitelist()
def export_view(view, fmt="csv", filters=None, search=None, sort=None, columns=None, limit=None):
	"""Ask for this view as a file. Returns AT ONCE; a worker drains it and the tab is told when it lands.

	IT STILL RE-RUNS `get_data`, in the worker. Not a second query, not a raw dump — so the rows are the
	caller's own (permission conditions), the columns are the caller's own (grain + role), and an export
	can never show what the list would not. A separate query here is how an export starts leaking the day
	a rule changes on one path and not the other.

	THE GATES ARE UNCHANGED and are applied HERE, in the request, so a refusal is still an error the
	person sees on the click rather than a job that fails silently a minute later:
	  * the view must be readable — the same check that opens it;
	  * the caller must hold the native EXPORT permission on the driving doctype, an ordinary role
	    permission an operator ticks, not a concept invented here;
	  * every download is written to frappe's `Access Log` — by the producer, once the file is real.

	WHY IT NO LONGER ANSWERS WITH THE FILE. Building it inline cost ~41.7s of SQL for one real view and
	one real Sales Manager, and died on the 120s gateway timeout. `tatva_connect.exports` says the rest.

	`limit` is how many rows the READER is looking at — the file is the screen unless they asked for
	everything, in which case it is absent and `row_cap()` is the only bound. It can only ever narrow.
	"""
	_assert_may_export(view)  # a gate, not a read: the queue path below asks it again for what it returns
	fmt = (fmt or "csv").lower()
	if fmt not in tabular.FORMATS:
		frappe.throw(_("Unsupported export format {0}.").format(fmt))

	return exports.queue("Smart View", view, fmt,
	                     {"filters": filters, "search": search, "sort": sort, "columns": columns,
	                      "limit": limit})


def produce_export(job, params, progress):
	"""The Smart View producer for `tatva_connect.exports` — see that module for the returned shape.

	THE GATES ARE ASKED AGAIN. A job row outlives the request that made it, so entitlement may have moved
	between the click and the drain; the worker re-reads rather than trusting a minute-old decision.

	PAGED, because `get_data` caps a page at PAGE_MAX — asking it for 5,000 rows silently returned 200 and
	the download looked complete. An export that quietly drops 667 of 867 rows is worse than one that
	refuses, so it walks the pages the same way a reader would and stops at a stated ceiling.

	THE CEILING IS THE OPERATOR'S, `exports.row_cap()` — the same one the list download reads, instead of a
	5,000 held here that answered "give me this list" 20x smaller than the list export did on the same site.
	"""
	d, driving_name = _assert_may_export(job.reference)

	# The reader asked for what is on screen, or for everything. Either way the operator's ceiling is the
	# last word — a limit cannot raise it, only stop short of it.
	cap = exports.row_cap()
	if asked := frappe.cint(params.get("limit")):
		cap = min(cap, asked)
	cols, rows = [], []
	# One label read per DISTINCT value per column, as the list download memoises it: a hundred thousand
	# rows of six stages cost six reads.
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
	# Only the CEILING truncates. A reader who asked for the rows on screen got exactly what they asked
	# for, and telling them it was cut short would be a lie about their own choice.
	truncated = len(rows) >= cap and not frappe.cint(params.get("limit"))
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
	"""A cell as a reader should see it — the same rule the list download applies (`list_export`).

	A Link at a grain master holds `programme::Archived` and the grid renders the label beside it, so a
	file that dumped the column handed a manager a spreadsheet the CRM never showed — and the two
	downloads of the same lead disagreed. `labels.shown_at` is the app's ONE answer to how a value reads;
	the column already names its target, so it is asked directly rather than re-derived from meta.

	`None` becomes empty rather than the string "None", which is what a reader would otherwise see. The
	formula guard is NOT here: it belongs to the file and is applied by `tabular.write` for every cell."""
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
	"""Whether to offer the download at all — the same native permission the export itself enforces, asked
	up front so the button is absent rather than present-and-refusing."""
	driving_name, _tbl = _driving(base_object)
	return bool(frappe.has_permission(driving_name, "export"))


@frappe.whitelist()
def delete_view(name):
	"""Delete a Smart View — the one write predicate: standard (or another user's) is operator-only."""
	doc = frappe.get_doc("CRM Smart View", name)
	if not sv_perms.can_write(doc):
		frappe.throw(_("You can only delete your own views."), frappe.PermissionError)
	frappe.delete_doc("CRM Smart View", name, ignore_permissions=True)  # authz-ok: tier-a — smart-view scaffolding, operator-run
	return {"deleted": name}
