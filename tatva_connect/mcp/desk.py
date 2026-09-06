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
import frappe
from frappe.desk.desktop import get_workspaces
from frappe.utils import get_url, slug

from tatva_connect.mcp import ToolError

def list_url(doctype):
	"""Where the records of a doctype are listed."""
	return f"{get_url()}/app/{slug(doctype)}"


def form_url(doctype, name="new"):
	"""One record's form. `new` is the blank one, which is where a configuration task starts."""
	return f"{get_url()}/app/{slug(doctype)}/{name}"


def workspace_url(workspace):
	return f"{get_url()}/app/{slug(workspace)}"


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
	if name not in [page["name"] for page in visible]:
		known = ", ".join(page["name"] for page in visible)
		raise ToolError(f"No workspace called '{name}' that this login can open. Available: {known}.")

	doc = frappe.get_doc("Workspace", name)
	return {
		"workspace": doc.name,
		"title": doc.label or doc.name,
		"url": workspace_url(doc.name),
		"shortcuts": [{"label": row.label, "doctype": row.link_to, "url": list_url(row.link_to)}
		              for row in doc.shortcuts if row.type == "DocType" and row.link_to],
		"links": [{"label": row.label, "doctype": row.link_to, "url": list_url(row.link_to)}
		          for row in doc.links if row.link_type == "DocType" and row.link_to and not row.hidden],
	}
