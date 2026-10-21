# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The tool catalog — the ONE declaration of what this server offers, and of the words it offers it in.

`server.py` never names a tool. It asks here for the listing published at `tools/list` and for the
handler behind a `tools/call`, so a tool reaches an agent by being registered here and by nothing
else. The `get_guide` tool and the `initialize` instructions are both GENERATED from this registry:
a tool cannot ship undocumented, and the manual cannot drift from the catalog.

Two closed vocabularies, in the shape `api/_base.ERROR_CODES` and `automation/registry` already use —
declared once, gated at import, so a new word cannot be invented quietly:

  VERBS      every tool is `<verb>_<subject>`, and the verb is one of three. `list` returns a map and
             takes no question, `search` answers a question, `get` returns one named thing. An agent
             that learns three verbs can predict the whole surface.
  ARGUMENTS  every argument is described ONCE, here, and a tool names the ones it takes. Two tools
             cannot describe `route` differently, because there is only one description of `route`.

A handler takes the call's arguments dict and returns a string or a JSON-able structure. It raises
`ToolError` to refuse; `server.py` turns that into a tool-level error result, which is what the
protocol asks for — a tool that refuses is a RESULT, not a transport fault.
"""
import json
from collections.abc import Callable
from dataclasses import dataclass

from frappe.utils import get_url

from tatva_connect.mcp import ToolError, desk, docs, palette, partner_api, records, schema, settings

# Re-exported so `server.py` catches one name and this module stays the only thing it talks to.
__all__ = ["ARGUMENTS", "REGISTRY", "VERBS", "Tool", "ToolError", "as_text", "find", "instructions", "listing"]

VERBS = frozenset({"list", "search", "get"})

# THE argument vocabulary — every input any tool accepts, described once, in the caller's language.
ARGUMENTS = {
	"space": {"type": "string", "description": "A space route, as returned by list_docs."},
	"query": {"type": "string", "description": "What to look for, in plain words."},
	"route": {"type": "string", "description": "A page route, as returned by list_docs or search_docs."},
	"page": {"type": "integer", "description": "Which page of a long answer, from 1. Defaults to 1."},
	"doctype": {"type": "string", "description": "A doctype name exactly as list_tables, search_schema or get_schema returns it."},
	"workspace": {"type": "string", "description": "A workspace name, as returned by list_workspaces."},
	"name": {"type": "string", "description": "A record's name exactly as search_records returns it. Leave it out for a single settings record."},
	"filters": {"type": "object", "description": 'Frappe filters as {fieldname: value} or {fieldname: [operator, value]}, fieldnames from get_schema. Example: {"vertical": "Goodflip", "status": ["in", ["Open", "Won"]]}.'},
	"node_type": {"type": "string", "description": "A node type exactly as list_node_types returns it."},
	"operation": {"type": "string", "description": "A Partner API endpoint's operation exactly as search_api returns it, such as lead_create."},
}

# The JSON types the vocabulary uses, as Python checks them; a bool is not an integer here.
JSON_TYPES = {"string": str, "integer": int, "object": dict}

SERVER_SUMMARY = (
	"The TatvaCare CRM documentation server for {site}. It reads the staff handbook, the live doctype "
	"schema, the Desk map, and the rows of only the setup tables an operator lists (list_tables) — all "
	"under the signed-in person's own permissions. It is READ-ONLY: it changes nothing."
)

HOW_TO_USE = (
	"Tools are <verb>_<subject>: `list` gives a map and asks nothing, `search` answers a question, `get` "
	"returns one named thing.\n"
	"How to do a task: search_docs (or list_docs) → get_doc for the steps → get_schema for the form, and "
	"list_workspaces → get_workspace for where it sits in the sidebar. Answer as numbered steps; each step "
	"names the screen and gives its `url` from a tool result. Never write a URL a tool did not return, not even as "
	"an example.\n"
	"How something is set up: list_tables (or search_schema for a word) → get_schema for its fields and "
	"relations → search_records → "
	"get_record for the one you need. Workflows: get_record on CRM Workflow carries its graph; "
	"list_node_types → get_node_type for the building blocks.\n"
	"How the Partner API works: search_api → get_api for an endpoint, and search_docs with space 'docs' for "
	"its guides.\n"
	"Be sparing: {budget}a page is {record_page_size} rows. "
	"Narrow with query or filters instead of paging through a table. A field with no value is left out.\n"
	"Quote what comes back exactly — it is what the person sees on screen. The person makes every change "
	"themselves in Desk, under their own login. get_guide repeats this text."
)


@dataclass(frozen=True)
class Tool:
	name: str
	description: str
	handler: Callable
	arguments: tuple = ()
	required: tuple = ()

	# Import-time vocabulary gate: an unnamed verb or an undeclared argument cannot reach the catalog.
	def __post_init__(self):
		verb = self.name.split("_")[0]
		if verb not in VERBS:
			raise ValueError(f"Tool {self.name!r} must start with one of {sorted(VERBS)} — see VERBS.")
		undeclared = [name for name in self.arguments if name not in ARGUMENTS]
		if undeclared:
			raise ValueError(f"Tool {self.name!r} takes {undeclared}, which ARGUMENTS does not declare.")
		unoffered = [name for name in self.required if name not in self.arguments]
		if unoffered:
			raise ValueError(f"Tool {self.name!r} requires {unoffered}, which it does not accept.")

	def refusal(self, arguments):
		"""The published inputSchema, held: an unknown, missing or mistyped argument is named, never guessed at."""
		unknown = sorted(set(arguments) - set(self.arguments))
		if unknown:
			return f"{self.name} takes no {', '.join(unknown)}. It takes: {', '.join(self.arguments) or 'nothing'}."
		missing = [name for name in self.required if arguments.get(name) in (None, "")]
		if missing:
			return f"{self.name} needs {', '.join(missing)}."
		for name, value in arguments.items():
			expected = ARGUMENTS[name]["type"]
			if value is not None and (isinstance(value, bool) or not isinstance(value, JSON_TYPES[expected])):
				return f"{name} must be {'an' if expected[0] in 'aeiou' else 'a'} {expected}."
		return None

	def schema(self):
		"""The MCP `inputSchema` — assembled from the argument vocabulary, never re-described."""
		return {
			"type": "object",
			"properties": {name: ARGUMENTS[name] for name in self.arguments},
			"required": list(self.required),
			"additionalProperties": False,
		}

	def publish(self):
		# Every tool reads only (tests/mcp/test_readonly_static.py holds it), so the hint is declared once, for all.
		return {"name": self.name, "title": self.name.replace("_", " ").capitalize(), "description": self.description,
		        "inputSchema": self.schema(),
		        "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}}


def _get_guide(_arguments):
	"""The manual, assembled from the registry so it can never list a tool that is not registered."""
	cfg = settings.config()
	# 0 is the operator's "unlimited", so no budget is stated rather than "0 calls".
	budget = f"about {cfg['per_user_rate']} calls every {cfg['window_seconds']} seconds per person; " if cfg["per_user_rate"] else ""
	return "\n\n".join([SERVER_SUMMARY.format(site=get_url()), HOW_TO_USE.format(budget=budget, **cfg)])


REGISTRY = (
	Tool(
		name="get_guide",
		description="How this server works and the order to call its tools in. Call it first if your client did not show the server's instructions.",
		handler=_get_guide,
	),
	Tool(
		name="list_docs",
		description=("The handbook map. With no space: every space, with its route and url. With a space: its sections "
		             "and pages in reading order, each with route and url. Use it to browse; use search_docs to find a page by words."),
		handler=docs.list_docs,
		arguments=("space",),
	),
	Tool(
		name="search_docs",
		description=("Find handbook pages by words in their title or body. Returns up to a few hits, each with title, "
		             "route, url and a snippet — never the whole page. Follow up with get_doc on the best route."),
		handler=docs.search_docs,
		arguments=("query", "space"),
		required=("query",),
	),
	Tool(
		name="get_doc",
		description=("Read one handbook page by its route: the steps a person follows, as text, plus its url. A long page "
		             "comes in numbered pages — `pages` says how many; ask for the next with `page`."),
		handler=docs.get_doc,
		arguments=("route", "page"),
		required=("route",),
	),
	Tool(
		name="search_schema",
		description=("Find the doctype or field behind a business word ('campaign', 'dropped reason'). Returns names only, "
		             "and `more` when the word matched too much. Follow up with get_schema."),
		handler=schema.search_schema,
		arguments=("query",),
		required=("query",),
	),
	Tool(
		name="get_schema",
		description=("One doctype's structure, never its rows: every field a person meets (label, type, options, when it "
		             "shows), the url of each Link's target, its child tables, the doctypes that link to it, its Desk urls, "
		             "and what this login may do with it (you_can). Works on a child table too."),
		handler=schema.get_schema,
		arguments=("doctype",),
		required=("doctype",),
	),
	Tool(
		name="list_workspaces",
		description="The Desk map: every workspace this login can open, with its url. Use it to say where in the sidebar something lives.",
		handler=desk.list_workspaces,
	),
	Tool(
		name="get_workspace",
		description="One workspace: the shortcuts and lists it puts on screen, each with the url that opens it.",
		handler=desk.get_workspace,
		arguments=("workspace",),
		required=("workspace",),
	),
	Tool(
		name="list_tables",
		description=("The setup tables whose rows this login may read, each with its list url and whether it is a single "
		             "settings record. Nothing outside this list is readable."),
		handler=records.list_tables,
	),
	Tool(
		name="search_records",
		description=("One page of rows from a table in list_tables: name, the list-view columns and each row's url. Narrow "
		             "with `query` (words in the title or search fields) and `filters`. When `more` is true, "
		             "narrow the filters instead of paging through the table."),
		handler=records.search_records,
		arguments=("doctype", "query", "filters", "page"),
		required=("doctype",),
	),
	Tool(
		name="get_record",
		description=("One record in full: every field its form shows, its child tables, the url of each Link value, and "
		             "the screens that open it (a Workflow or Task Type also gets its CRM app url; a Workflow carries its "
		             "graph). Call it only for the record you will use."),
		handler=records.get_record,
		arguments=("doctype", "name"),
		required=("doctype",),
	),
	Tool(
		name="list_node_types",
		description="The building blocks of a workflow, one line each (type, label, category, description). For people who can read workflows.",
		handler=palette.list_node_types,
	),
	Tool(
		name="get_node_type",
		description="One workflow node type in full: its settings, its outputs, and the url of its handbook page when one is published.",
		handler=palette.get_node_type,
		arguments=("node_type",),
		required=("node_type",),
	),
	Tool(
		name="search_api",
		description=("Find Partner API endpoints by words ('create lead', 'ticket comments'). Returns each endpoint's "
		             "operation, method, path, summary and /docs url. The Partner API guides (quickstart, errors, rate "
		             "limits) are the 'docs' space of search_docs and get_doc."),
		handler=partner_api.search_api,
		arguments=("query",),
		required=("query",),
	),
	Tool(
		name="get_api",
		description=("One Partner API endpoint in full: its parameters, request body, responses and errors, with shared "
		             "definitions filled in, and its /docs url. Call it for the endpoint you will explain."),
		handler=partner_api.get_api,
		arguments=("operation",),
		required=("operation",),
	),
)


def listing():
	"""What `tools/list` publishes."""
	return [tool.publish() for tool in REGISTRY]


def find(name):
	"""The tool by name, or None. `server.py` decides what a miss means."""
	return next((tool for tool in REGISTRY if tool.name == name), None)


def instructions():
	"""The `initialize` instructions — the same manual, so an agent knows before its first call."""
	return _get_guide({})


def as_text(value):
	"""One text block. A structured answer is JSON inside it; agents read both, humans read one."""
	return value if isinstance(value, str) else json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)
