# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The SHORTCUT surface of the spotlight: the named things a person can open, beside the records they find.

READ LIVE, NEVER INDEXED. A record is found in the FTS index because there are 173k of them; a shortcut
is a named piece of a person's own setup and there are tens. Each source carries its OWN gate — the
surface a preset sits on, crm's `user == "" or mine`, the CRM Workflow permission hook, frappe's
`is_permitted`, Insights' app-and-row pair — while the index has one permission model built for patient
rows, so indexing shortcuts would restate five gates in a sixth place.

SMART VIEWS ARE NOT HERE: the panel reads them from the `tatva-smart-views` store, which reloads the
moment one is created, renamed, reordered or shared, so a resting spotlight can never show a stale tab.

EVERY SOURCE IS A LISTER THIS APP ALREADY HAS, so a source cannot disagree with the surface it names.
Adding a sixth is one entry in `_SOURCES` and one function.
"""
import frappe
from frappe import _
from frappe.utils.data import quoted

from tatva_connect import presets

# An EXTERNAL route is a Desk page: it leaves this app, so the row says so rather than looking native.
KIND_PRESET = "Preset"
KIND_LIST_VIEW = "List View"
KIND_WORKFLOW = "Workflow"
KIND_WORKSPACE = "Workspace"
KIND_DASHBOARD = "Dashboard"

# What each kind is called as a HEADING. Declared, never pluralised in code — a heading is a word, not a rule.
GROUP = {
	# Both are a saved way of looking at a list; two headings made a reader choose between synonyms.
	KIND_PRESET: "Saved Views",
	KIND_LIST_VIEW: "Saved Views",
	KIND_WORKFLOW: "Workflows",
	KIND_WORKSPACE: "Workspaces",
	KIND_DASHBOARD: "Insights",
}

_LIMIT = 100


def _shortcut(kind, label, route, context="", external=False, icon=""):
	"""`icon` is the row's OWN glyph where the surface lets an author pick one; blank means the kind's.

	A context equal to the label is dropped: a row that prints its own name twice reads as a defect, and
	the panel already says what a contextless row is."""
	return {
		"kind": kind,
		"group": _(GROUP[kind]),
		"label": label,
		"context": "" if context == label else context,
		"route": route,
		"external": external,
		"icon": icon,
	}


def _presets():
	"""A person's saved filter-and-sort on a surface. Personal by construction, so the only question left
	is whether they can still OPEN the surface — which `presets.may_use` answers, not this module.

	Only a Smart View surface is offered: that is the one surface the presets control is mounted on, and a
	route invented for a surface that cannot read it lands a person on an unfiltered page.

	The route carries the preset's STATE, not its name, in the `?filters=`/`?sort=` the dashboard drill
	already uses — so the list applies it through the one arrival path it already has."""
	from tatva_connect.smartview.permissions import SMART_VIEW_DT

	rows = frappe.get_all(  # authz-ok: tier-a — this seam's own rows, filtered to the session user
		presets.DOCTYPE,
		filters={"user": frappe.session.user, "reference_doctype": SMART_VIEW_DT},
		fields=["name", "label", "reference_doctype", "reference_name", "filters", "sort"],
		limit=100,
	)
	rows = [row for row in rows if presets.may_use(row.reference_doctype, row.reference_name)]
	# The view a preset belongs to, named the way a person named it — the raw PK places nothing.
	labels = dict(frappe.get_all(
		SMART_VIEW_DT, filters={"name": ("in", [row.reference_name for row in rows] or [""])},
		fields=["name", "label"], as_list=True,
	))
	return [
		_shortcut(KIND_PRESET, row.label,
		        {"name": "SmartViews", "query": _drill(row)},
		        context=labels.get(row.reference_name) or row.reference_name)
		for row in rows
	]


def _drill(row):
	"""The view, plus the preset's own filters and sort in the drill's shapes — a dict and an order_by."""
	query = {"view": row.reference_name, "filters": row.filters or "{}"}
	sort = frappe.parse_json(row.sort) if row.sort else ""
	if sort:
		query["sort"] = sort
	return query


def _list_views():
	"""Saved list views, through crm's own reader — `user == "" or mine` is its rule, not ours.

	`route_name` is the view's OWN destination and is not a required field, so a row without one is
	skipped rather than sent somewhere: a guess would open a Deals view on the Leads page."""
	from crm.api.views import get_views

	return [
		_shortcut(KIND_LIST_VIEW, view.get("label") or view["name"],
		        {"name": view["route_name"],
		         "params": {"viewType": view.get("type") or "list"},
		         "query": {"view": view["name"]}},
		        context=view["route_name"], icon=view.get("icon") or "")
		for view in get_views("")
		if view.get("label") and not view.get("is_standard") and view.get("route_name")
	]


def _workflows():
	"""Journey definitions. `get_list` is already narrowed by the CRM Workflow permission_query_conditions
	hook, so the grain a caller may see decides this and nothing here restates it.

	The doctype has `workflow_name` and `lifecycle_state` — NOT `title`/`status`, which `get_list` drops
	silently rather than raising, so asking for them cost every row its second line."""
	return [
		_shortcut(KIND_WORKFLOW, row.workflow_name or row.name,
		        {"name": "Workflow", "params": {"workflowId": row.name}},
		        context=row.lifecycle_state or "")
		for row in frappe.get_list(
			"CRM Workflow", fields=["name", "workflow_name", "lifecycle_state"], limit=100
		)
	]


def _workspaces():
	"""Desk workspaces, through frappe's own sidebar reader: it filters domain and blocked modules in SQL,
	then asks `is_permitted()` per row against the session's roles.

	The URL is built the way `frappe.get_desk_link` builds one — `quoted(slug(...))` under the desk path,
	which the desk router resolves against `frappe.workspaces`. Neither the path nor the casing is ours to
	invent: `slug` lowercases and hyphenates, and `/desk` is where v16 serves it."""
	from frappe.desk.desktop import get_workspaces
	from frappe.desk.utils import slug

	return [
		_shortcut(KIND_WORKSPACE, page.get("title") or page["name"],
		        f"/desk/{quoted(slug(page.get('title') or page['name']))}",
		        context=page.get("module") or "", external=True)
		for page in get_workspaces().get("pages") or []
		if page.get("public") and not page.get("is_hidden")
	]


def _insights():
	"""Insights dashboards, through Insights' own two gates and neither restated here.

	`check_app_permission` is the app-level one — without `Insights User` or `Insights Admin` a
	`get_list` RAISES, so this is asked first rather than letting a rep's every spotlight log a
	traceback. The row-level one is Insights' `permission_query_conditions` hook, which `get_list`
	applies for us. A bench without the app installed answers nothing, the way a dormant integration does."""
	if "insights" not in frappe.get_installed_apps():
		return []
	from insights.permissions import check_app_permission

	if not check_app_permission():
		return []
	rows = frappe.get_list("Insights Dashboard v3", fields=["name", "title", "workbook"], limit=100)
	# The workbook a dashboard sits in is what places it, read through the same gate that offered it.
	books = {str(row.workbook) for row in rows if row.workbook}
	titles = {
		str(book.name): book.title
		for book in (frappe.get_list("Insights Workbook", filters={"name": ("in", list(books))},
		                             fields=["name", "title"], limit=100) if books else [])
	}
	return [
		_shortcut(KIND_DASHBOARD, row.title or row.name, f"/insights/dashboards/{quoted(row.name)}",
		        context=titles.get(str(row.workbook)) or "", external=True)
		for row in rows
	]


# Declaration order is the order a tie is shown in: a person's own setup before the app's furniture.
_SOURCES = (_presets, _list_views, _workflows, _workspaces, _insights)


@frappe.whitelist()
@frappe.read_only()
def shortcuts(limit=_LIMIT):
	"""Everything this caller may open, from every source, each already gated by its own.

	The WHOLE list, unfiltered: there are tens of it, so the panel fetches once and narrows as a person
	types rather than asking the server per keystroke. Matching a label is not a job for an index.

	A source that throws is LOGGED AND SKIPPED. One surface being unavailable must not cost a person the
	other four, and the spotlight is the place they go when something else is already wrong."""
	found = []
	for source in _SOURCES:
		try:
			found += source()
		except Exception:
			# On the replica the log itself is a refused INSERT, and the skip becomes the 500 it exists to prevent.
			frappe.write_only()(frappe.log_error)(title=_("Spotlight shortcut source failed"), message=frappe.get_traceback())
	return {"shortcuts": found[: frappe.utils.cint(limit) or _LIMIT]}
