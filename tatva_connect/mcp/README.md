# MCP documentation server

A read-only [Model Context Protocol](https://modelcontextprotocol.io) server: an AI agent belonging
to a member of staff reads the handbook, the live doctype schema and the Desk map, and is told where
to go. It reads the rows of only the setup tables an operator lists, and changes nothing.

Plans: `docs/plans/search/2026-09-04-mcp-docs-server/`, `docs/plans/search/2026-10-19-mcp-masters-and-relations/`.

## Shape

| File | Holds |
|---|---|
| `server.py` | The Streamable HTTP transport and the JSON-RPC dispatch. `handle()` is the protocol and knows nothing about Frappe's request; `endpoint()` is the adapter |
| `tools.py` | The catalog, and the two closed vocabularies — the verbs a tool may be named with, and the arguments a tool may take |
| `docs.py` | The handbook: `Wiki Space` and `Wiki Document`, as the publish script left them |
| `schema.py` | Doctype structure from `frappe.get_meta`, scoped to the allowlisted apps |
| `desk.py` | Workspaces, and the ONE place a URL is built: Desk lists and forms, handbook pages, and a record's CRM app screen (`notifications.deep_link.app_url`) |
| `records.py` | Rows of the doctypes listed in Tatva Platform Settings → Access → Data Access for AI Agents → Readable Tables |
| `palette.py` | The workflow node types, cut from `workflow_engine.registry.node_types()` |

## Address and access

One endpoint, `/mcp` (nginx rewrites it to `tatva_connect.mcp.server.endpoint`). Authentication is
Frappe's own, so the call runs as that user under that user's permissions:

- **OAuth** — the user adds the address to their agent and clicks Connect. Frappe's 401 challenge,
  discovery documents, client registration and login page (`frappe.integrations.oauth2`, OAuth
  Settings) do the rest, and the agent calls with a bearer token.
- **API key** — `Authorization: token api_key:api_secret`, for scripts.

There is no MCP role and no MCP user: any staff login may connect, which is the default allowed role
Frappe gives a registered OAuth client.

Dormant until an operator enables `MCP::Docs::server`. Off, every call answers "not enabled" and runs
no query.

## The three rules

Enforced by `tests/mcp/test_readonly_static.py`, which walks the syntax tree:

1. **Nothing writes.** No `save`, `insert`, `db.set_value` — and no `frappe.log_error`, which is a
   master write inside a request switched to the read replica. Diagnostics go to `frappe.logger`.
2. **Rows only from the listed tables.** Not listed is not readable, and an empty list reads nothing.
   Reads go through `get_list` / `get_doc` + `check_permission` + field levels, never `get_all`, SQL or
   a lock; Password and hidden fields are never sent.
3. **No caller input reaches SQL as an identifier.**

## Adding a tool

Register it in `tools.REGISTRY`. The name must be `<verb>_<subject>` with a verb from `VERBS`, and
every argument must be declared once in `ARGUMENTS` — both gated at import. The manual (`get_guide`)
and the `initialize` instructions are generated from the registry, so a tool cannot ship
undocumented.
