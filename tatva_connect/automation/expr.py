"""The ONE shared expression resolver - the value-resolution seam for Set Field / Create Task /
Add Comment when their value mode is `Expression`.

Reuses `frappe.safe_eval` - the same restricted-execution mechanism Notification.condition,
AssignmentRule.condition, and Workflow transition conditions already use. Builtins are stripped by
Frappe; the ONLY extra globals exposed here are the 7 date-math helpers an author needs for the
realistic use cases (stage transitions, computed dates). `ctx` (the rule's activity context) is the
sole local. No `eval()`, no `exec()`, no string-formatted SQL — the safe_eval SANDBOX is identical to
stock (same stripped builtins + whitelist).

Authorship (separation of duties): expression authoring follows create/write on CRM Automation Rule —
System Manager and the custom **Automation Manager** role (defined in fixtures/role.json, desk_access).
This is deliberate: ops author rules within the SysMgr-curated field allowlist (CRM Automation Field is
SysMgr-write-only), and the safe_eval sandbox bounds what an expression can do. So authoring is wider
than stock Notification/Workflow (SysMgr-only) but doubly bounded — the allowlist limits *which* fields,
the sandbox limits *what* an expression can execute. "Automation Manager" is NOT a Frappe core role (it
authorizes nothing in frappe/frappe); it is our own ops-automation role.

Author input example: `add_days(ctx["date_2"], 3)`. The Literal / From Context modes stay untouched
(plain Data fields, no evaluation) - Expression is strictly opt-in, chosen only when math is needed.
"""
import ast

import frappe

# The only extra globals an author may reach - the date helpers. Mirrors the set Notification /
# Workflow already whitelist; nothing more is exposed, so the trust boundary does not widen.
_SAFE_GLOBALS = {
	"add_days": frappe.utils.add_days,
	"add_months": frappe.utils.add_months,
	"add_years": frappe.utils.add_years,
	"add_to_date": frappe.utils.add_to_date,
	"getdate": frappe.utils.getdate,
	"get_datetime": frappe.utils.get_datetime,
	"nowdate": frappe.utils.nowdate,
}


def resolve_expression(expr, context):
	"""Evaluate an author-written expression against the rule's context via `frappe.safe_eval`.
	`ctx` is the only local; date-math helpers are the only extra globals. Raises on a bad/unsafe
	expression so the caller's per-action guard records it and rolls the rule back (savepoint).

	`context` is a `refs.Values` — a REAL mapping, not a dict. An author writes `add_days(ctx["date_2"], 3)`
	and now writes `add_days(ctx["crm_lead.date_2"], 3)`; that is an ordinary string subscript, so it parses
	and `context_keys` extracts it unchanged. What it needs is an object that can ANSWER it, and a flat dict
	of namespaced keys cannot: the subject is never copied into journey state, so `crm_lead.date_2` is served
	off the live document by the resolver. `safe_eval` calls `__getitem__` on the local exactly as it would
	on a dict — the sandbox is untouched and no new global is exposed."""
	return frappe.safe_eval(expr, dict(_SAFE_GLOBALS), {"ctx": context})


def assert_parses(expr):
	"""Syntax-only check used at rule SAVE time (Action.validate) - NOT a live `resolve_expression()`
	dry run. An empty author-time context would make any `ctx["field"]` reference raise `KeyError`
	and wrongly block a legitimate rule at save time, so we only confirm the expression is a single
	eval-form Python expression. Raises `SyntaxError` on a malformed expression."""
	ast.parse(expr, mode="eval")


def references_context(expr):
	"""True when the expression reads `ctx`. A context-FREE expression is a constant, so an author-time
	validator may evaluate it for real and reject a bad value; one that reads `ctx` can only be checked
	for syntax, because the author-time context is empty and `ctx.get("x")` would legitimately yield
	`None` there. Structural (an AST walk), never a substring match - `context_note` must not read as a
	reference."""
	return any(isinstance(node, ast.Name) and node.id == "ctx" for node in ast.walk(ast.parse(expr, mode="eval")))


def context_keys(expr):
	"""Which context keys an expression READS - `ctx["x"]`, `ctx.get("x")`, `ctx.get("x", d)`.

	The other half of `emits`: a node declares what it writes, and this is how the same question is
	answered for what it reads, so publish can refuse a reference nothing upstream produces. Structural,
	like `references_context` - a substring hunt would find `ctx` inside a quoted string and miss
	`ctx . get ( "x" )`. A dynamic key (`ctx[name]`) is not a literal and is skipped rather than guessed:
	better to check nothing than to invent a name that was never written.
	"""
	found = set()
	for node in ast.walk(ast.parse(expr, mode="eval")):
		if isinstance(node, ast.Subscript) and _is_ctx(node.value):
			if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
				found.add(node.slice.value)
		elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and _is_ctx(node.func.value):
			if node.func.attr == "get" and node.args:
				first = node.args[0]
				if isinstance(first, ast.Constant) and isinstance(first.value, str):
					found.add(first.value)
	return found


def dict_literal_keys(expr):
	"""The literal string keys of an expression that IS a dict literal, else None.

	A Set Variables node writes whatever its expression evaluates to. When that expression is written the
	way every author writes it - `{"stage": "Qualified"}` - its keys are knowable without running it, so
	the values it contributes downstream can be named rather than described as opaque. None means "cannot
	be enumerated", which the caller must treat as unknown rather than as empty.
	"""
	tree = ast.parse(expr, mode="eval").body
	if not isinstance(tree, ast.Dict):
		return None
	keys = set()
	for key in tree.keys:
		if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
			return None  # one computed key and the whole set is unknowable
		keys.add(key.value)
	return keys


def _is_ctx(node):
	return isinstance(node, ast.Name) and node.id == "ctx"
