# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The workflow palette — the node types a workflow is built from, as the builder itself reads them.

`workflow_engine.registry.node_types()` is the one source the canvas palette, inspector and validator
share; this module only cuts it in two, because the whole catalogue is ~50K characters. It is open to
whoever may read CRM Workflow, as the builder is.
"""
from tatva_connect.mcp import ToolError, desk, schema

SUMMARY = ("type", "label", "category", "description")


def _palette():
	if not schema.can_read("CRM Workflow"):
		raise ToolError("Workflow node types are for people who can read workflows, and this login cannot.")
	from tatva_connect.workflow_engine.registry import node_types

	return node_types()


# -- tools --------------------------------------------------------------------
def list_node_types(_arguments):
	"""Every node type, one line each."""
	return {"node_types": [{key: node[key] for key in SUMMARY} for node in _palette()],
	        "next": "Call get_node_type for one type's settings and outputs."}


def get_node_type(arguments):
	"""One node type in full: its settings, its outputs, and its handbook page when one is published."""
	wanted = (arguments.get("node_type") or "").strip()
	palette = _palette()
	node = next((node for node in palette if node["type"] == wanted), None)
	if not node:
		raise ToolError(f"No node type '{wanted}'. Known: {', '.join(node['type'] for node in palette)}.")
	return {**node, "docs_url": desk.page_url(node["docs_route"])} if node.get("docs_route") else node
