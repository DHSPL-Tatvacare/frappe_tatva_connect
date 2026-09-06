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

from tatva_connect.mcp import ToolError, desk, docs, schema

# Re-exported so `server.py` catches one name and this module stays the only thing it talks to.
__all__ = ["ARGUMENTS", "REGISTRY", "VERBS", "Tool", "ToolError", "as_text", "find", "instructions", "listing"]

VERBS = frozenset({"list", "search", "get"})

# THE argument vocabulary — every input any tool accepts, described once, in the caller's language.
ARGUMENTS = {
	"space": {"type": "string", "description": "A space route, as returned by list_docs."},
	"query": {"type": "string", "description": "What to look for, in plain words."},
	"route": {"type": "string", "description": "A page route, as returned by list_docs or search_docs."},
	"page": {"type": "integer", "description": "Which page of a long document. Defaults to 1."},
	"doctype": {"type": "string", "description": "A doctype name, as returned by search_schema."},
	"workspace": {"type": "string", "description": "A workspace name, as returned by list_workspaces."},
}

SERVER_SUMMARY = (
	"The TatvaCare CRM documentation server. It reads the staff handbook, the live doctype schema "
	"and the Desk navigation map of https://one.tatvacare.in, and it is READ-ONLY: it holds no "
	"records, cannot change anything, and cannot show leads, tasks, calls or patients."
)

HOW_TO_USE = (
	"How to use it. Every tool is named <verb>_<subject>: `list` gives a map and asks nothing, "
	"`search` answers a question, `get` returns one named thing. Start with list_docs for the map or "
	"search_docs for a question in words, then get_doc on the route you picked. Quote what comes back "
	"exactly — it is read live, so it is what the person will see on screen. Never invent a URL: use "
	"the ones the tools return. The person makes every change themselves in Desk, under their own "
	"login and their own permissions."
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

	def schema(self):
		"""The MCP `inputSchema` — assembled from the argument vocabulary, never re-described."""
		return {
			"type": "object",
			"properties": {name: ARGUMENTS[name] for name in self.arguments},
			"required": list(self.required),
			"additionalProperties": False,
		}

	def publish(self):
		return {"name": self.name, "description": self.description, "inputSchema": self.schema()}


def _get_guide(_arguments):
	"""The manual, assembled from the registry so it can never list a tool that is not registered."""
	lines = [SERVER_SUMMARY, "", HOW_TO_USE, "", "Tools:"]
	lines += [f"  {tool.name} — {tool.description}" for tool in REGISTRY]
	return "\n".join(lines)


REGISTRY = (
	Tool(
		name="get_guide",
		description="How this server works and which tool to call in what order. Call this first.",
		handler=_get_guide,
	),
	Tool(
		name="list_docs",
		description="The handbook map: every space, with its sections and pages in reading order.",
		handler=docs.list_docs,
		arguments=("space",),
	),
	Tool(
		name="search_docs",
		description="Find handbook pages by words in their title or body. Returns routes and snippets.",
		handler=docs.search_docs,
		arguments=("query", "space"),
		required=("query",),
	),
	Tool(
		name="get_doc",
		description="Read one handbook page by its route. Long pages come back in numbered pages.",
		handler=docs.get_doc,
		arguments=("route", "page"),
		required=("route",),
	),
	Tool(
		name="search_schema",
		description="Find the doctype or field behind a business word. Returns names to look up.",
		handler=schema.search_schema,
		arguments=("query",),
		required=("query",),
	),
	Tool(
		name="get_schema",
		description="One doctype in full: every field a person meets, and the Desk pages showing them.",
		handler=schema.get_schema,
		arguments=("doctype",),
		required=("doctype",),
	),
	Tool(
		name="list_workspaces",
		description="The Desk map: every workspace this login can open, with its URL.",
		handler=desk.list_workspaces,
	),
	Tool(
		name="get_workspace",
		description="One workspace: the shortcuts and lists it puts on screen, each as a URL to open.",
		handler=desk.get_workspace,
		arguments=("workspace",),
		required=("workspace",),
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
	return value if isinstance(value, str) else json.dumps(value, indent=1, default=str)
