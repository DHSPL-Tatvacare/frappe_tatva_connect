# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The handbook tools — the published Wiki, read as it stands.

The handbook FILES are the source of truth; the publish script writes them into `Wiki Space` and
`Wiki Document`, and those two doctypes are what this module reads. That mirror is regenerated on
every publish, and a page's `route` is its identity inside it, so a route is the stable address an
agent can hold on to and come back with.

Everything goes through `frappe.get_list`, never `get_all`: the list call is the one that applies the
caller's own permissions, and the caller is a real person with a real role. The tree order comes from
Wiki's own nested set, so the map an agent reads is the order a reader sees in the sidebar.

Two shapes are handled once, here, rather than in each tool: page content arrives with screenshots
inlined as data URIs (stripped to a short marker — an agent cannot read base64, and one page of it
would swamp any client's result cap), and the route Wiki stores may or may not carry a leading slash.
"""
import re

import frappe
from frappe.utils.nestedset import get_descendants_of

from tatva_connect.mcp import ToolError, settings

SPACE_FIELDS = ["name", "route", "space_name", "root_group"]
NODE_FIELDS = ["name", "title", "route", "is_group"]

# Screenshots are inlined <img src="data:..."> by the publisher; diagrams are mermaid text and stay.
_IMG_WITH_ALT = re.compile(r'<img[^>]*\balt="([^"]*)"[^>]*>')
_IMG_ANY = re.compile(r"<img[^>]*>")


# -- shared reads -------------------------------------------------------------
def _spaces(route=None):
	filters = {"route": route} if route else {}
	return frappe.get_list("Wiki Space", filters=filters, fields=SPACE_FIELDS,
	                       order_by="switcher_order asc, space_name asc", limit=0)


def _space(route):
	"""One space by route, or a refusal that names the routes that do exist."""
	found = _spaces(route)
	if found:
		return found[0]
	known = ", ".join(space.route for space in _spaces()) or "none published yet"
	raise ToolError(f"No space at route '{route}'. Spaces on this site: {known}.")


def _nodes(space, pages_only=False):
	"""Every node under a space, in sidebar order. Empty when the space holds nothing yet."""
	names = get_descendants_of("Wiki Document", space.root_group) or []
	if not names:
		return []
	filters = {"name": ["in", names]}
	if pages_only:
		filters["is_group"] = 0
	return frappe.get_list("Wiki Document", filters=filters, fields=NODE_FIELDS,
	                       order_by="lft asc", limit=0)


def _content(name):
	"""The page body, cleaned. Read one row at a time — a page of base64 is not list material."""
	return _clean(frappe.db.get_value("Wiki Document", name, "content") or "")


def _clean(content):
	text = _IMG_WITH_ALT.sub(lambda m: f"[image: {m.group(1)}]" if m.group(1) else "[image]", content)
	return _IMG_ANY.sub("[image]", text)



def _chunks(text, limit):
	"""Whole lines, in bounded pages — every client caps a result, and none cap it the same."""
	pages, current, size = [], [], 0
	for line in text.splitlines(keepends=True):
		if size + len(line) > limit and current:
			pages.append("".join(current))
			current, size = [], 0
		current.append(line)
		size += len(line)
	pages.append("".join(current))
	return pages


# -- tools --------------------------------------------------------------------
def list_docs(arguments):
	"""The map: every space, and its sections and pages in the order a reader sees them."""
	route = (arguments.get("space") or "").strip()
	spaces = [_space(route)] if route else _spaces()
	return {"spaces": [{
		"space": space.space_name,
		"route": space.route,
		"contents": [{"title": node.title, "route": node.route, "section": bool(node.is_group)}
		             for node in _nodes(space)],
	} for space in spaces]}


def search_docs(arguments):
	"""Pages matching the words asked for — through Wiki's OWN search, not a scan of our own.

	`wiki...wiki_document.search.search` is the same indexed search the Wiki UI runs: a SQLite
	full-text index with scores and highlighted snippets, and it drops any hit in a space the caller
	cannot open. Re-implementing that with a LIKE scan would rank worse and would have to re-derive
	space visibility by hand. Its `space` argument is a space's ROOT GROUP, so a route from
	`list_docs` is resolved to one here."""
	from wiki.frappe_wiki.doctype.wiki_document.search import search as wiki_search

	query = (arguments.get("query") or "").strip()
	if not query:
		raise ToolError("Give a word or phrase to search for.")

	route = (arguments.get("space") or "").strip()
	try:
		found = wiki_search(query, _space(route).root_group if route else None)
	except Exception:
		raise ToolError("The handbook search index is not available on this site.")

	return {"query": query, "hits": [{
		"title": hit["title"],
		"route": hit["route"],
		"snippet": _clean(hit.get("content") or ""),
		"score": hit.get("score"),
	} for hit in found["results"][:settings.config()["search_max_hits"]]]}


def get_doc(arguments):
	"""One page by route, in bounded pages. `page` walks a long document; `pages` says how many."""
	route = (arguments.get("route") or "").strip().strip("/")
	if not route:
		raise ToolError("Give the route of a page — list_docs and search_docs both return routes.")

	rows = frappe.get_list("Wiki Document", filters={"route": ["in", [route, f"/{route}"]], "is_group": 0},
	                       fields=NODE_FIELDS, limit=1)
	if not rows:
		raise ToolError(f"No page at route '{route}'. Use search_docs to find the right one.")

	try:
		asked = int(arguments.get("page") or 1)
	except (TypeError, ValueError):
		raise ToolError("page must be a whole number.")

	pages = _chunks(_content(rows[0].name), settings.config()["page_characters"])
	wanted = max(1, min(asked, len(pages)))
	return {"title": rows[0].title, "route": rows[0].route,
	        "page": wanted, "pages": len(pages), "text": pages[wanted - 1]}
