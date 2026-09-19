# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The CRM app's Help centre panel, served from the published handbook.

The panel used to render a hand-written list of nine sections pointing at `docs.frappe.io/crm` — the
upstream product's own public documentation, not ours. This replaces that list with the wiki this
site actually publishes, so a section added, renamed or removed in the handbook is simply what the
next open returns. Nothing is cached and nothing is copied, so there is no sync to keep.

Reads go through `tatva_connect.mcp.docs`, which already walks the same two doctypes for the MCP
server. One reader, one order, one permission story.

THE FENCE CARRIES ITSELF. Wiki registers `permission_query_conditions` and `has_permission` for both
`Wiki Space` and `Wiki Document`, and its `can_read_space` intersects the caller's roles with the
space's own role table. Because `docs._spaces`/`_nodes` use `frappe.get_list`, a caller is handed the
spaces they may open and no others: this endpoint needs no role check of its own, and adding one
would be a second answer to a question the platform already answers.
"""
import frappe

from tatva_connect.mcp import docs


@frappe.whitelist()
def tree():
	"""The handbook as the Help centre needs it: every space the caller may open, its sections, their pages.

	Shape is deliberately ours, not the panel's: `route` is carried on every node so a richer panel can
	navigate in place later without a second endpoint.
	"""
	return {"spaces": [_space_tree(space) for space in docs._spaces()]}


def _space_tree(space):
	"""Sections and their pages, in the order the sidebar shows them.

	Sidebar order is a WALK, not a column: `sort_order` is assigned per sibling group, so reading the
	flat list by `sort_order` interleaves branches and reading it by `lft` ignores a reorder that never
	rebuilt the nested set. Walking from the root with siblings sorted is the only shape that matches
	what a reader sees.
	"""
	children = {}
	for node in docs._nodes(space):
		children.setdefault(node.parent_wiki_document, []).append(node)
	for kids in children.values():
		kids.sort(key=lambda n: (n.sort_order or 0, n.title or ""))

	sections, loose = [], []

	def walk(parent, section):
		for node in children.get(parent, []):
			if node.is_group:
				# The DEEPEST group a page sits under names it: the panel renders two levels, and a
				# specific shelf reads better than the broad one it hangs from.
				bucket = {"title": node.title, "route": _route(node), "pages": []}
				sections.append(bucket)
				walk(node.name, bucket)
			else:
				(section["pages"] if section else loose).append(_page(node))

	walk(space.root_group, None)
	out = [section for section in sections if section["pages"]]
	if loose:
		out.insert(0, {"title": space.space_name, "route": _route(space), "pages": loose})
	return {"title": space.space_name, "route": _route(space), "sections": out}


def _page(node):
	return {"title": node.title, "route": _route(node)}


def _route(node):
	return (node.route or "").lstrip("/")
