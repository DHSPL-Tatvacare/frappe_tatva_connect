# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Who may read a Lead or a Deal, written down once instead of computed per query.

THE PROBLEM. crm answers "may this user read this lead" with a DISJUNCTION over two relations
(`crm/permissions/org_hierarchy.py:54`): `lead_owner = me OR name IN (<ToDo subquery>)`. A B-tree serves
conjunctive seeks on one relation; it cannot serve a disjunction whose second branch lives in another
table, so MariaDB must test every row. Measured on prod (172,923 leads) for a rep owning ONE lead:
169,733 rows read, 352 ms, 0.0006% of them wanted. The same predicate is wrapped as a subquery by
`access/visibility.py` for every ViaParent doctype, so one shape costs eight surfaces.

THE FIX. Collapse the two relations into one, so the predicate has a single branch and an index can
serve it: `name IN (SELECT reference_name FROM tabCRM Record Access WHERE user = me AND ...)`. The same
shape measured 0.02 ms. Cost becomes O(rows the user may see), which is the correct complexity for
row-level security and does not degrade as the table grows.

WHAT IT STORES. DIRECT grants only — one row per (user, record) for the record's owner and for every
live assignee. A manager's subtree is expanded at QUERY time into `user IN (me, ...subtree)`, never
materialised: an org change would otherwise rewrite thousands of rows, and the subtree is bounded by
headcount either way.

FAIL-CLOSED, AND FAIL-BACK. `condition()` returns None whenever it is not certain — switch off, doctype
not covered, crm's own rule says unrestricted — and the caller then uses crm's original function. A
bug here degrades to today's behaviour (correct, slow), never to a wider read. It never returns "" for
a restricted user, because "" means unrestricted and that is the one answer that leaks.

IT DOES NOT DECIDE THE RULE. Who counts as privileged, whether the hierarchy is on, and who is in a
subtree are all asked of crm's own functions. This module is an INDEX of crm's rule, not a second
opinion about it; the day crm changes the rule, `tests/access/test_record_access_equivalence` fails.
"""
import frappe
from crm.permissions.org_hierarchy import _OWNER_FIELD, _team_mem_query, hierarchy_enabled

from tatva_connect import automation
from tatva_connect.access import request_cache

# The dormant operator toggle. Off, every caller falls back to crm and this table is written but unread.
SWITCH = "Access::Record::acl"

DOCTYPE = "CRM Record Access"

# The doctypes this index covers, and the column each names its owner in — crm's own map, never retyped.
SUBJECTS = _OWNER_FIELD


def viewers(doctype: str, name: str) -> set[str]:
	"""Every user holding a DIRECT grant on this record: its owner, plus every live assignee.

	Mirrors the two branches of crm's predicate exactly — the owner column it names, and a ToDo that is
	not Cancelled. Nothing else grants, because nothing else appears in the rule being indexed."""
	owner_field = SUBJECTS.get(doctype)
	if not owner_field:
		return set()
	users = set()
	owner = frappe.db.get_value(doctype, name, owner_field)
	if owner:
		users.add(owner)
	users.update(
		frappe.get_all(
			"ToDo",
			filters={"reference_type": doctype, "reference_name": name, "status": ["!=", "Cancelled"]},
			pluck="allocated_to",
		)
	)
	return {u for u in users if u}


def sync(doctype: str, name: str) -> None:
	"""Make the table agree with `viewers()` for one record. Idempotent, and writes only the difference.

	Runs INLINE in the caller's transaction and is deliberately not enqueued: a queued write would let a
	list read run against a grant that has already changed, which for a permission index is not staleness
	but a wrong answer."""
	if not (doctype in SUBJECTS and name):
		return
	# The bulk lane is skipped for the same reason the search index skips it (index.py:645): a re-migration
	# saves every lead, and three queries per row is a quarter of a million round trips on one worker. The
	# recovery is `rebuild`, which is why it is a command and not a patch.
	if frappe.flags.in_import or frappe.flags.in_migrate or frappe.flags.in_install:
		return
	want = viewers(doctype, name)
	have = {
		row.user: row.name
		for row in frappe.get_all(
			DOCTYPE,
			filters={"reference_doctype": doctype, "reference_name": name},
			fields=["name", "user"],
		)
	}
	for user in want - set(have):
		frappe.get_doc(
			{"doctype": DOCTYPE, "user": user, "reference_doctype": doctype, "reference_name": name}
		).insert(ignore_permissions=True)
	revoked = [have[user] for user in set(have) - want]
	if revoked:
		# db.delete, not delete_doc: these rows are a derived index with no controller, no links and no
		# on_trash, so the document API would only buy link validation nobody asked for.
		frappe.db.delete(DOCTYPE, {"name": ["in", revoked]})


def readers(user: str) -> list[str]:
	"""The users whose grants this caller inherits: themselves, plus their subtree when the hierarchy is on.

	Asked of crm's own subtree query rather than re-derived, so a change to what a manager may see cannot
	be true in one place and false here."""
	def build():
		users = [user]
		if hierarchy_enabled():
			users += [row[0] for row in _team_mem_query(user).run() if row[0]]
		return sorted(set(users))

	# Request-cached like `visibility.match_conditions`: `condition()` is called once per list query per
	# doctype, and the subtree cannot change inside one request.
	return request_cache("tatva_connect:record_access_readers", user, build)


def condition(doctype: str, user: str, force: bool = False) -> str | None:
	"""The WHERE string for this doctype, or None when this module declines to answer.

	None is the safe answer and the common one: it means the caller falls back to crm's original
	function, so a disarmed switch, an uncovered doctype or anything unexpected costs performance and
	never correctness.

	`force` is for the audit only: it builds the condition while the switch is still off, so the two
	rules can be compared on live data before either is trusted."""
	if doctype not in SUBJECTS or not (force or automation.is_enabled(SWITCH)):
		return None
	people = readers(user)
	if not people:
		return None
	names = ", ".join(frappe.db.escape(u) for u in people)
	return (
		f"`tab{doctype}`.`name` in (select `reference_name` from `tab{DOCTYPE}`"
		f" where `reference_doctype` = {frappe.db.escape(doctype)} and `user` in ({names}))"
	)


def on_subject_saved(doc, method=None):
	"""CRM Lead / CRM Deal after_insert + on_update — the owner half of the grant.

	No @fail_safe on purpose. Everywhere else in this app a dropped side effect is recoverable, so
	fail-safe is right; here the side effect IS the permission. Letting it raise rolls the save back with
	it, so a record's owner and the index of who may read it move together or not at all."""
	if doc.doctype in SUBJECTS and doc.has_value_changed(SUBJECTS[doc.doctype]):
		sync(doc.doctype, doc.name)


def on_subject_deleted(doc, method=None):
	"""CRM Lead / CRM Deal after_delete — the record is gone, so every grant on it goes with it.

	Not a visibility hole (a deleted record matches nothing), but an orphan grant is garbage that
	accumulates for ever and makes the audit's `extra` column meaningless. after_delete, not on_trash,
	for the same reason the assignment leg uses it: on_trash still sees the row."""
	if doc.doctype in SUBJECTS:
		frappe.db.delete(DOCTYPE, {"reference_doctype": doc.doctype, "reference_name": doc.name})


def on_assignment(doc, method=None):
	"""ToDo after_insert / on_trash — the assignment half. A cancelled ToDo grants nothing, hence the
	sibling below reacting to `status` as well as to a reallocation."""
	if doc.reference_type in SUBJECTS and doc.reference_name:
		sync(doc.reference_type, doc.reference_name)


def on_assignment_change(doc, method=None):
	"""ToDo on_update — only a reallocation or a status move can change who the record is visible to."""
	if doc.has_value_changed("allocated_to") or doc.has_value_changed("status"):
		on_assignment(doc, method)


def install() -> None:
	"""Swap the SHAPE of crm's restrictive condition, never its meaning.

	crm answers "" for Administrator, System Manager and a Sales Manager outside the tree — "" means
	unrestricted, and that privilege logic stays entirely crm's. This wrapper asks crm FIRST and returns
	its answer untouched whenever it is empty; only a restrictive answer is replaced, and only with the
	fast form of the same set. So the blast radius is one query shape, not the rule.

	Anything unexpected — the switch off, an import that moved, a raised exception — returns crm's own
	answer, which is correct and merely slow. There is no path here that widens a read.

	Idempotent: patching twice would nest the wrappers and ask crm twice, so the flag guards it."""
	from crm.permissions import org_hierarchy

	if getattr(org_hierarchy, "_tatva_record_access_installed", False):
		return

	def wrap(original, doctype):
		# The signature MIRRORS crm's exactly and takes no **kwargs. frappe calls these as
		# `frappe.call(fn, user, doctype=...)`, and frappe.call passes only the arguments the target
		# declares — so a wrapper that accepts **kwargs is handed `doctype` and forwards it into a
		# function that never took it. That is a TypeError on every list read, for everyone, switch off
		# or on; it is invisible to any test that calls these functions directly instead of through
		# frappe.call.
		def resolve(user=None):
			answer = original(user)
			if not answer:
				return answer  # crm says unrestricted; privilege logic is not ours to touch
			try:
				return condition(doctype, user or frappe.session.user) or answer
			except Exception:
				frappe.log_error(frappe.get_traceback(), "record_access: falling back to crm")
				return answer

		return resolve

	org_hierarchy.get_lead_permission_query_conditions = wrap(
		org_hierarchy.get_lead_permission_query_conditions, "CRM Lead"
	)
	org_hierarchy.get_deal_permission_query_conditions = wrap(
		org_hierarchy.get_deal_permission_query_conditions, "CRM Deal"
	)
	org_hierarchy._tatva_record_access_installed = True


def rebuild(doctype: str | None = None) -> dict:
	"""Recompute the whole table from the live grants. RE-RUNNABLE, on purpose — this is not a patch.

	A patch is recorded in Patch Log and never runs again, which is wrong for this: a re-migration
	rewrites lead owners wholesale, and the index has to be rebuilt to match. So it is a command:

	    bench --site <site> execute tatva_connect.access.record_access.rebuild
	    bench --site <site> execute tatva_connect.access.record_access.rebuild --kwargs "{'doctype':'CRM Deal'}"

	RECONCILES, does not just insert. It writes the missing rows AND deletes the ones no longer earned —
	an insert-only refill would leave a re-migrated lead's previous owner holding a grant for ever, which
	is the exact failure this table would be blamed for.

	Reads the two source relations whole (two queries per doctype) rather than calling `sync` per record,
	because 172,923 records one at a time is a quarter of a million round trips for the same answer."""
	targets = [doctype] if doctype else list(SUBJECTS)
	report = {}
	for dt in targets:
		owner_field = SUBJECTS.get(dt)
		if not (owner_field and frappe.db.table_exists(dt)):
			continue

		want = {
			(row.u, row.name)
			for row in frappe.db.sql(
				f"select `name`, `{owner_field}` as u from `tab{dt}`"
				f" where `{owner_field}` is not null and `{owner_field}` != ''",
				as_dict=True,
			)  # sqli-ok: owner_field is crm's own _OWNER_FIELD value, never a caller's
		}
		want |= {
			(row.u, row.n)
			for row in frappe.db.sql(
				"select `allocated_to` as u, `reference_name` as n from `tabToDo`"
				" where `reference_type` = %s and `status` != 'Cancelled'"
				" and `allocated_to` is not null and `reference_name` is not null",
				(dt,),
				as_dict=True,
			)
		}
		have = {
			(row.user, row.reference_name): row.name
			for row in frappe.get_all(
				DOCTYPE,
				filters={"reference_doctype": dt},
				fields=["name", "user", "reference_name"],
				limit_page_length=0,
			)
		}

		missing = sorted(want - set(have))
		if missing:
			frappe.db.bulk_insert(
				DOCTYPE,
				["name", "user", "reference_doctype", "reference_name"],
				[(frappe.generate_hash(length=10), u, dt, n) for u, n in missing],
				ignore_duplicates=True,
			)
		stale = [have[key] for key in set(have) - want]
		for chunk in (stale[i : i + 500] for i in range(0, len(stale), 500)):
			frappe.db.delete(DOCTYPE, {"name": ["in", chunk]})

		frappe.db.commit()
		report[dt] = {"granted": len(want), "added": len(missing), "removed": len(stale)}
	print(frappe.as_json(report))
	return report
