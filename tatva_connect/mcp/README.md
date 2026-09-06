# MCP documentation server

A read-only [Model Context Protocol](https://modelcontextprotocol.io) server: an AI agent belonging
to a member of staff reads the handbook, the live doctype schema and the Desk map, and is told where
to go. It returns no record of any kind and changes nothing.

Design: vault `Projects/frappe-crm/05-roadmap/08-mcp-docs-server.md`.
Build order: `docs/plans/2026-09-04-mcp-docs-server.md`.

## Shape

| File | Holds |
|---|---|
| `server.py` | The Streamable HTTP transport and the JSON-RPC dispatch. `handle()` is the protocol and knows nothing about Frappe's request; `endpoint()` is the adapter |
| `tools.py` | The catalog, and the two closed vocabularies — the verbs a tool may be named with, and the arguments a tool may take |
| `docs.py` | The handbook: `Wiki Space` and `Wiki Document`, as the publish script left them |
| `schema.py` | Doctype structure from `frappe.get_meta`, scoped to the allowlisted apps |
| `desk.py` | Workspaces, and the ONE place a Desk URL is built |

## Address and access

One endpoint, `/mcp` (nginx rewrites it to `tatva_connect.mcp.server.endpoint`). Authentication is
Frappe's own — `Authorization: token <key>:<secret>` — so the call runs as that user under that
user's permissions. There is no second credential and no OAuth server.

Dormant until an operator enables `MCP::Docs::server`. Off, every call answers "not enabled" and runs
no query.

## The three rules

Enforced by `tests/mcp/test_readonly_static.py`, which walks the syntax tree:

1. **Nothing writes.** No `save`, `insert`, `db.set_value` — and no `frappe.log_error`, which is a
   master write inside a request switched to the read replica. Diagnostics go to `frappe.logger`.
2. **No tool returns a row.** The absence of the capability is the guarantee; a filter can be wrong,
   a missing tool cannot.
3. **No caller input reaches SQL as an identifier.**

## Adding a tool

Register it in `tools.REGISTRY`. The name must be `<verb>_<subject>` with a verb from `VERBS`, and
every argument must be declared once in `ARGUMENTS` — both gated at import. The manual (`get_guide`)
and the `initialize` instructions are generated from the registry, so a tool cannot ship
undocumented.
