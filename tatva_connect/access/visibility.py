"""Row visibility — the ONE brain for "which RECORDS may an internal user see".

Sibling of `entitlement.py` (which FIELDS). crm scopes Leads/Deals through the org hierarchy but never
their child records, so an agent otherwise sees every Task, Call Log and Note in the business. This
restores parity, natively — no fork, no re-derivation of crm's own scope.

THE MODEL: a doctype DECLARES how it is scoped; nothing re-decides it.

    SCOPED["CRM Task"] = Scope(switch=…, by=[Own("owner", "assigned_to"), ViaParent("reference_doctype", …)])

A `Strategy` answers the same question two ways and nothing else — `clause()` for a list read,
`admits()` for one row — and a doctype's strategies are ORed. Everything that is NOT the doctype's own
linkage is written ONCE, in the two entry points: the switch gate, the privilege short-circuit,
fail-open-when-off, and fail-closed-to-`1=0` when nothing admits. Adding a doctype is a row in `SCOPED`.

WHY THIS SHAPE, WRITTEN DOWN SO IT IS NOT UNDONE. There used to be a hand-maintained `PARENT_OF` of
near-identical resolver functions, a `_VIA` beside it, a `_link_columns` that PROBED the schema to
rediscover what those resolvers already knew, and — after `CRM Workflow` needed scoping by its own grain
rather than through a parent — a whole parallel family (`grain_admits`, `grain_readable_names`,
`grain_scoped_pqc`, `grain_scoped_has_permission`) that duplicated the switch gate, the privilege check,
the fail-open rule and the `1=0` rule a second time. `smartview/permissions.py` had duplicated the same
grain rule a third time. One question, three implementations, and the drift lock that was supposed to
protect it merely asserted the split was tidy. A fourth doctype would have been a fourth copy: that is
how three of these tables sat unscoped for months while every docstring claimed otherwise.

The ptype contract: visibility ALWAYS asks the READ scope. A child's own action level (read/write/delete)
is its role docperm — so a write needs role-write on the child AND read-visibility on its parent. No
per-ptype branching here.

Fail-closed: an unresolvable parent for a non-owner denies. Fail-OPEN on the switch is deliberate and is
the seam's contract — dormant-by-default means stock crm until an operator arms it.
"""
import frappe
from frappe.model.db_query import DatabaseQuery
from frappe.query_builder import DocType
from pypika.terms import PseudoColumn

from tatva_connect import automation
from tatva_connect.access import request_cache

PARENT_DOCTYPES = ("CRM Lead", "CRM Deal")


def is_privileged(user=None):
	"""Administrator or System Manager. The ONE spelling — `smartview.is_operator` delegates here."""
	user = user or frappe.session.user
	return user == "Administrator" or "System Manager" in frappe.get_roles(user)


def _q(value):
	return frappe.db.escape(value)


def _name_in(doctype, names):
	"""`name in (…)` for a strategy that decides in Python. None when it admits nothing, so the caller
	can tell "this strategy contributes no rows" from "it contributes every row"."""
	if not names:
		return None
	return f"`tab{doctype}`.`name` in ({', '.join(_q(n) for n in names)})"


class Strategy:
	"""One way a row may reach a caller. `fields` are the columns `admits` reads, so a caller that
	fetches rows itself can ask for exactly the right shape and never find a key missing.

	`context()` is per-SWEEP state — something worth resolving once when judging many rows, such as the
	set of names DocShare hands this user. It is deliberately NOT a request cache: a memo keyed on the
	user goes stale the moment a share or an entitlement changes inside one request, which is a gate
	silently answering yesterday's question. Built fresh by `sweep_context`, handed down, thrown away.
	"""

	fields = ()

	def context(self, doctype, user):
		return None

	def clause(self, doctype, user):
		raise NotImplementedError

	def admits(self, row, doctype, user, ctx=None):
		raise NotImplementedError


class Own(Strategy):
	"""It is the caller's own record. `columns` are the self-ownership markers this doctype really has —
	declared, never probed: CRM Task has `assigned_to`, Call Log/Note/WhatsApp have only `owner`, and a
	Smart View spells it `owner_user`."""

	def __init__(self, *columns):
		self.fields = columns or ("owner",)

	def clause(self, doctype, user):
		return " or ".join(f"`tab{doctype}`.`{c}`={_q(user)}" for c in self.fields)

	def admits(self, row, doctype, user, ctx=None):
		return any((row.get(c) or None) == user for c in self.fields)


class ViaParent(Strategy):
	"""It hangs off a Lead or Deal the caller may read. The dynamic-link column PAIR is declared once and
	both halves derive from it — the schema probe that used to rediscover it existed only because this
	map was implicit."""

	def __init__(self, type_column, name_column):
		self.type_column, self.name_column = type_column, name_column
		self.fields = (type_column, name_column)

	def clause(self, doctype, user):
		tbl = f"`tab{doctype}`"
		return " or ".join(
			f"({tbl}.`{self.type_column}`={_q(p)} and {tbl}.`{self.name_column}` in "
			f"{_visible_parent_subquery(p, user)})"
			for p in PARENT_DOCTYPES
		)

	def resolve(self, row):
		"""(parent_doctype, parent_name) or None. The linkage itself, for a caller that wants the PARENT
		rather than a verdict — the search index asks which lead a row belongs to."""
		if row.get(self.type_column) not in PARENT_DOCTYPES or not row.get(self.name_column):
			return None
		return row.get(self.type_column), row.get(self.name_column)

	def admits(self, row, doctype, user, ctx=None):
		parent = self.resolve(row)
		return parent_readable(parent[0], parent[1], user) if parent else False


class ViaSibling(Strategy):
	"""It reaches its Lead only through ANOTHER scoped row — a Step Log is visible exactly when its
	Journey is. Both halves recurse into the sibling's own strategies, so the rule is stated once and the
	two can never answer differently.

	The sibling's own SWITCH is deliberately not consulted: the question being answered is THIS doctype's
	switch, and a Step Log scoped while Journeys are not is a narrower answer, never a wider one."""

	def __init__(self, column, parent_doctype):
		self.column, self.parent_doctype = column, parent_doctype
		self.fields = (column,)

	def clause(self, doctype, user):
		inner = _compose(self.parent_doctype, user)
		return (
			f"`tab{doctype}`.`{self.column}` in "
			f"(select `name` from `tab{self.parent_doctype}` where {inner})"
		)

	def admits(self, row, doctype, user, ctx=None):
		name = row.get(self.column)
		if not name:
			return False
		parent = frappe.db.get_value(
			self.parent_doctype, name, _row_fields(self.parent_doctype), as_dict=True,
		)
		return bool(parent) and row_admits(parent, self.parent_doctype, user)


class Shared(Strategy):
	"""Frappe's own DocShare handed it over. Never grain-filtered: someone who could share it decided this
	person should have it, so scoping it away would make cross-line sharing silently do nothing."""

	def context(self, doctype, user):
		return _shared_names(doctype, user)

	def clause(self, doctype, user):
		return _name_in(doctype, self.context(doctype, user))

	def admits(self, row, doctype, user, ctx=None):
		name = row.get("name")
		shared = self.context(doctype, user) if ctx is None else ctx
		return bool(name) and name in shared


class RuleGrain(Strategy):
	"""The row DECLARES a business line, and it overlaps the caller's entitlement.

	For a row that is a RULE rather than a record — a workflow, a saved view. Its grain is a RULE grain:
	authored, and free to leave an axis BLANK meaning ANY. So it is asked of
	`entitlement.grain_overlaps_entitlement`, never `grain_entitled`, which takes a record's DATA grain and
	would read that blank as the literal empty string. That is the defect that once hid 129 fields from
	1,894 leads with every test green, and it is why nothing here compares a grain tuple.

	Declaring NO line at all is not "a rule about nothing" — it is site-wide, shown to everyone and asked
	of nobody's entitlement.

	`only_when` names a column that must be truthy for this strategy to apply at all: a Smart View's grain
	governs STANDARD views, and a personal one reaches its owner or nobody.

	It decides in Python and answers as a name list, because the wildcard semantics live in
	`taxonomy.grain` and writing them a second time in SQL is the twin this module exists to prevent.
	These tables hold tens of rows.
	"""

	def __init__(self, *axes, only_when=None):
		self.axes, self.only_when = axes, only_when
		self.fields = (*axes, *((only_when,) if only_when else ()))

	def clause(self, doctype, user):
		# `get_all` deliberately: it ignores permissions, so building the clause cannot recurse back into
		# the hook that is asking for it. Every row is then gated by `admits` below.
		rows = frappe.get_all(doctype, fields=["name", *self.fields])  # authz-ok: tier-b — gated row by row below
		return _name_in(doctype, [r.name for r in rows if self.admits(r, doctype, user)])

	def admits(self, row, doctype, user, ctx=None):
		from tatva_connect.access import entitlement  # local: entitlement reads this module's siblings

		if self.only_when and not row.get(self.only_when):
			return False
		grain = tuple((row.get(axis) or "") for axis in self.axes)
		if not any(grain):
			return True
		return entitlement.grain_overlaps_entitlement(grain, user=user)


class Scope:
	"""How one doctype is scoped. `switch` None means NOT switchable — enforced always, which is right
	only where the app's own endpoints are the granting door and nothing ever shipped it dormant."""

	def __init__(self, switch, by):
		self.switch, self.by = switch, by

	def fields(self):
		seen = []
		for strategy in self.by:
			seen += [f for f in strategy.fields if f not in seen]
		return tuple(seen)

	def armed(self):
		return True if self.switch is None else automation.is_enabled(self.switch)


# THE REGISTRY. A doctype is scoped iff it is here, and how it is scoped is this row and nothing else.
SCOPED = {
	"CRM Task": Scope(
		"Task::CRM Task::visibility",
		[Own("owner", "assigned_to"), ViaParent("reference_doctype", "reference_docname")],
	),
	"CRM Call Log": Scope(
		"Telephony::CRM Call Log::visibility",
		[Own(), ViaParent("reference_doctype", "reference_docname")],
	),
	"FCRM Note": Scope(
		"Note::FCRM Note::visibility",
		[Own(), ViaParent("reference_doctype", "reference_docname")],
	),
	# frappe_whatsapp's own doctype names the link target `reference_name`, not `reference_docname`.
	"WhatsApp Message": Scope(
		"WhatsApp::WhatsApp Message::visibility",
		[Own(), ViaParent("reference_doctype", "reference_name")],
	),
	# A journey carries its lead's field values in `state_json` and a signal its payload; unscoped, anyone
	# who could open these lists read every other grain's lead data.
	"CRM Workflow Journey": Scope(
		"Workflow::CRM Workflow Journey::visibility",
		[Own(), ViaParent("subject_doctype", "subject_name")],
	),
	"CRM Workflow Signal": Scope(
		"Workflow::CRM Workflow Signal::visibility",
		[Own(), ViaParent("subject_doctype", "subject_name")],
	),
	# A Step Log has `subject_name` and NO `subject_doctype`, so it cannot name its own parent — it is
	# visible exactly when its Journey is.
	"CRM Workflow Step Log": Scope(
		"Workflow::CRM Workflow Step Log::visibility",
		[Own(), ViaSibling("journey", "CRM Workflow Journey")],
	),
	# The Definition is a RULE, not a record hanging off a lead: it has no parent, and `Own` alone would
	# show a rep only the workflows they personally authored, which is not what "may see" means for a rule
	# that governs their own patients. It names the fields it reads and the messages it sends, so unscoped
	# it tells everyone how every other business line runs.
	"CRM Workflow": Scope(
		"Workflow::CRM Workflow::visibility",
		[RuleGrain("trigger_vertical", "trigger_group", "trigger_program")],
	),
	# Not switchable, and deliberately so: the SPA endpoints are the granting door (the doctype's DocPerms
	# are System-Manager-only), so this has never been dormant and a switch would imply it could be.
	"CRM Smart View": Scope(
		None,
		[Own("owner_user"), Shared(), RuleGrain("vertical", "group", "program", only_when="is_standard")],
	),
}


def _shared_names(doctype, user):
	"""The names frappe's own DocShare hands this user. Asked FRESH every time — see `Strategy.context`
	for why this must never become a request memo. A sweep resolves it once through `sweep_context`."""
	return set(frappe.share.get_shared(doctype, user) or [])


def _compose(doctype, user):
	"""The OR of every strategy's clause for one doctype, switch and privilege already decided.

	Nothing admitting selects NOTHING. `1=0` is the only honest answer: returning "" would silently widen
	a scoped list to the whole table, which is the failure mode this module exists to prevent."""
	parts = [c for c in (s.clause(doctype, user) for s in SCOPED[doctype].by) if c]
	return "(" + " or ".join(parts) + ")" if parts else "1=0"


def sweep_context(doctype, user=None):
	"""Each strategy's per-sweep state, resolved once, for a caller judging many rows of one doctype.
	Hand it to `row_admits`; never hold it across requests."""
	user = user or frappe.session.user
	return {s: s.context(doctype, user) for s in SCOPED[doctype].by}


def row_admits(row, doctype, user=None, ctx=None):
	"""May this caller see this row — the ONE predicate, asked of a row the caller already holds.

	Privilege first, so an operator never depends on holding an entitlement or a share. `ctx` is an
	optional `sweep_context`; without it every strategy resolves its own state fresh, which is the correct
	answer for a single row and the only safe default."""
	user = user or frappe.session.user
	if is_privileged(user):
		return True
	scope = SCOPED.get(doctype)
	if not scope:
		return False
	return any(s.admits(row, doctype, user, ctx.get(s) if ctx else None) for s in scope.by)


def scoped_pqc(doctype, user=None):
	"""The `permission_query_conditions` hook: list scoping for `doctype`.

	Privileged, switch-OFF, or a doctype nobody declared -> "" (no extra conditions; stock crm)."""
	scope = SCOPED.get(doctype)
	if not scope or not scope.armed():
		return ""
	user = user or frappe.session.user
	if is_privileged(user):
		return ""
	return _compose(doctype, user)


def scoped_has_permission(doc, ptype, user):
	"""The `has_permission` hook: the single-doc / deep-link gate, mirroring the list rule so a row on a
	hidden parent cannot be opened by name.

	Deny-only by construction — a controller cannot GRANT what the DocPerms withhold (frappe
	`permissions.py:481`) — so an undeclared doctype or a disarmed switch answers True."""
	doctype = doc.get("doctype") if doc else None
	scope = SCOPED.get(doctype)
	if not scope or not scope.armed():
		return True
	return row_admits(doc, doctype, user or frappe.session.user)


def _row_fields(doctype):
	"""Exactly the columns this doctype's predicate reads, deduped. `name` and `owner` always ride along:
	`Shared` keys on the name and a fetched row is compared by owner wherever `Own` is declared."""
	seen = ["name", "owner"]
	return seen + [f for f in SCOPED[doctype].fields() if f not in seen]


def parent_of(row, doctype):
	"""Which Lead/Deal this row hangs off, or None — the LINKAGE without the verdict.

	Read off the doctype's own declaration, so a caller that needs the parent (the search index, deciding
	which lead a hit belongs to) uses the same column pair the gate does. It used to import the resolver
	function directly, which is how a private helper became a second consumer nobody could see.
	"""
	scope = SCOPED.get(doctype)
	if not scope:
		return None
	for strategy in scope.by:
		if isinstance(strategy, ViaParent):
			found = strategy.resolve(row)
			if found:
				return found
	return None


def parent_readable(parent_doctype, parent_name, user=None):
	"""May `user` READ this parent Lead/Deal? The rule every child defers to, written once.

	A read surface that already KNOWS the parent — the journey-history endpoints, whose whole argument is
	one lead — asks it directly instead of synthesising a child row to be resolved back again.

	Missing and unreadable both answer False, so a caller cannot tell a record that is not there from one
	that is not theirs. Privilege is answered by `frappe.has_permission` itself; there is deliberately no
	second privilege check here.
	"""
	if not parent_doctype or not parent_name:
		return False
	if not frappe.db.exists(parent_doctype, parent_name):
		return False
	return bool(frappe.has_permission(parent_doctype, "read", parent_name, user=user))


# ---------------------------------------------------------------------------------------------------
# The QUERY side: applying frappe's own row gate to a query this app assembled itself. A different
# concern from the registry above — that decides the rule, this carries it into hand-built SQL.
# ---------------------------------------------------------------------------------------------------


def match_conditions(doctype, user=None):
	"""THE row gate: the SQL core applying to this doctype's own list for this user. "" = unrestricted.

	`build_match_conditions` is the whole of it — User Permissions, the owner constraint, shares — and it
	calls the app's `permission_query_conditions` hooks on the way. Asking `get_permission_query_conditions`
	alone returns only the hook half, which is how Smart Views once handed a user scoped to one vertical
	every lead in the system. One question, one API, one place. Request-cached per (doctype, user)."""
	user = user or frappe.session.user

	def build():
		try:
			return DatabaseQuery(doctype, user=user).build_match_conditions(as_condition=True) or ""
		except frappe.PermissionError:
			return "1=0"  # no read access at all -> select nothing

	return request_cache("tatva_connect:match_conditions", (doctype, user), build)


def readable_criterion(doctype, table, user=None):
	"""`table.name IN (the rows this user may read)` — the row gate as a qb criterion, or None when
	unrestricted.

	Every query this app builds ITSELF must AND this in. Frappe applies the gate inside its own list
	layer, so anything assembled in `frappe.qb` bypasses it entirely — that is not an oversight to guard
	against, it is the contract of building your own query.

	Scoped through a subquery rather than ANDed raw: the conditions name the doctype's own columns
	UNQUALIFIED, so the moment a query joins a child table those bare `name`/`owner` collide (MySQL 1052).
	Inside a single-table subquery they resolve cleanly, and the meaning is identical — the conditions only
	ever constrain that doctype's own rows."""
	cond = match_conditions(doctype, user)
	if not cond:
		return None
	src = DocType(doctype)
	sub = frappe.qb.from_(src).select(src.name).where(PseudoColumn(f"({cond})"))  # sqli-ok: framework-built conditions, no user value
	return table.name.isin(sub)


def scope(query, doctype, table, user=None):
	"""AND the row gate onto a query this app built itself. The applicator; `readable_criterion` is the rule.

	Every hand-built `frappe.qb` query over a scoped doctype goes through here. Frappe gates its own list
	layer, so a query assembled by hand is ungated by definition — the dashboard charts counted every lead
	and every task in the system for whoever asked, because nothing had ever ANDed this in."""
	criterion = readable_criterion(doctype, table, user)
	return query if criterion is None else query.where(criterion)


def _visible_parent_subquery(parent, user):
	"""The parent rows this user may read, as a SQL '(select name ...)', for `ViaParent`. Same gate as
	`match_conditions`, shaped as a string because a PQC hook returns SQL, not a criterion."""
	cond = match_conditions(parent, user)
	where = f" and ({cond})" if cond else ""
	return f"(select `name` from `tab{parent}` where 1=1{where})"
