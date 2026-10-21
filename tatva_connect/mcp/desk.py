# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The Desk map — the pages a person actually opens, as real URLs on this site.

Every URL an agent ever quotes is built HERE and nowhere else, so a guess about how Desk addresses a
doctype cannot appear in two shapes. `schema.py` asks this module for a doctype's pages rather than
composing its own, which is why an agent can say "open this" and be right.

The Desk route for a name comes from `frappe.utils.slug`, the framework's own — the same helper
`api/landing.py` already uses to answer where a user lands.

Which workspaces a caller may open is FRAPPE'S answer, not ours: `frappe.desk.desktop.get_workspaces`
is what Desk itself calls to draw the sidebar, and it applies three rules a plain `get_list` applies
none of — the user's blocked modules, the site's active domains, and the workspace's own role list. A
workspace's shortcuts and links are its child rows, read once the parent has passed that check; they
are that workspace, not separate objects with separate permissions.
"""
from urllib.parse import urlencode

import frappe
from frappe.desk.desktop import Workspace, get_workspaces
from frappe.desk.utils import is_item_allowed
from frappe.utils import get_url, get_url_to_form, get_url_to_list, slug
from frappe.utils.data import quoted

from tatva_connect.mcp import ToolError
from tatva_connect.notifications.deep_link import app_url


def list_url(doctype, filters=None):
	"""Where the records of a doctype are listed — frappe's own Desk address, pre-filtered by any exact-match filters."""
	exact = {field: value for field, value in (filters or {}).items() if not isinstance(value, dict | list)}
	return get_url_to_list(doctype) + (f"?{urlencode(exact)}" if exact else "")


def form_url(doctype, name="new"):
	"""One record's form. `new` is the blank one, which is where a configuration task starts."""
	return get_url_to_form(doctype, name)


def record_urls(doctype, name):
	"""Every screen that opens one record: its Desk form, and the CRM app screen it lives in when it has one."""
	urls = {"form": form_url(doctype, name)}
	if app := app_url(doctype, name):
		urls["app"] = app
	return urls


def page_url(route):
	"""A handbook page as the wiki serves it (wiki's own canonical_url: the site plus the route)."""
	return get_url("/" + (route or "").strip("/"))


def workspace_url(workspace):
	"""A workspace's Desk address, built as search/shortcuts builds it."""
	return get_url(f"/desk/{quoted(slug(workspace))}")


def urls_for(doctype):
	"""The ONE answer to 'where do I go for this doctype' — used by every tool that names a page."""
	return {"list": list_url(doctype), "new": form_url(doctype)}


def _visible():
	"""The workspaces Desk itself would show this caller, in the order Desk shows them."""
	return [page for page in get_workspaces().get("pages") or []
	        if page.get("public") and not page.get("is_hidden")]


# -- tools --------------------------------------------------------------------
def list_workspaces(_arguments):
	"""Every Desk workspace this caller can open, with the URL that opens it."""
	return {"workspaces": [{
		"workspace": page["name"],
		"title": page.get("label") or page.get("title") or page["name"],
		"url": workspace_url(page["name"]),
	} for page in _visible()]}


def get_workspace(arguments):
	"""One workspace: the shortcuts and links it puts on screen, each as a URL to open."""
	name = (arguments.get("workspace") or "").strip()
	if not name:
		raise ToolError("Name a workspace — list_workspaces returns the ones this login can see.")

	visible = _visible()
	page = next((page for page in visible if page["name"] == name), None)
	if not page:
		known = ", ".join(page["name"] for page in visible)
		raise ToolError(f"No workspace called '{name}' that this login can open. Available: {known}.")

	# Desk's own view: the site's customisation merged in, and each item kept only if is_item_allowed lets this login see it.
	ws = Workspace(page, minimal=True)
	doc = ws.doc
	return {
		"workspace": doc.name,
		"title": doc.label or doc.name,
		"url": workspace_url(doc.name),
		"shortcuts": [{"label": row.label, "doctype": row.link_to, "url": list_url(row.link_to)}
		              for row in doc.shortcuts
		              if row.type == "DocType" and row.link_to and is_item_allowed(row.link_to, row.type, ws)],
		"links": [{"label": row.label, "doctype": row.link_to, "url": list_url(row.link_to)}
		          for row in doc.links if row.link_type == "DocType" and row.link_to and not row.hidden
		          and is_item_allowed(row.link_to, row.link_type, ws)],
	}
