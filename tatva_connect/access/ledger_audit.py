# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Intent vs reality — what the ledger SAYS this site should be, against what it currently IS.

THE MODEL. `ledger.py` declares intent and knows nothing about this site. This module reads the live
schema and the live permission matrix and answers three questions, and only these three:

  * WHAT IS ENFORCED NOW — resolved the way Frappe resolves it, which is Custom DocPerm when any row
    exists for the doctype, else the app's own DocPerm. Reading `tabDocPerm` alone reports a false
    leak on every doctype we have already locked.
  * WHAT THE LEDGER WOULD MAKE IT — `ledger.rows_for`, which answers for any doctype, declared or not.
  * WHICH UNDECLARED DOCTYPES MATTER FIRST — ranked by evidence, not by alphabet.

THIS MODULE NEVER WRITES. Not a Custom DocPerm, not a Property Setter, not a flag. Phase 1 ships the
measurement so Phase 2's first enforcement run can be proven a no-op before it is allowed to be a
change. `lockdown.py` is the only module in this package that writes a permission row.

RANKING, AND WHY IT IS EVIDENCE. An undeclared doctype is scored by what is TRUE of it on this site —
does it carry an executable field, is it currently open to `All`/`Guest` write, does it hold rows —
never by how dangerous its name sounds. The three signals are independent and are reported
separately so a reviewer can disagree with the weighting and still use the facts.

Run:
    bench --site <site> execute tatva_connect.access.ledger_audit.report
"""

import frappe

from tatva_connect.access import ledger


def enforced_rows(doctype):
	"""The matrix Frappe actually enforces for `doctype` today, resolved the way Frappe resolves it."""
	source = "Custom DocPerm" if frappe.db.exists("Custom DocPerm", {"parent": doctype}) else "DocPerm"
	rows = frappe.get_all(
		source,
		filters={"parent": doctype, "permlevel": 0},
		fields=["role", "`read`", "`write`", "`create`", "`delete`", "if_owner"],
	)
	return {r.role: (r.read, r.write, r.create, r.delete, r.if_owner) for r in rows}


def parent_doctypes():
	"""Every doctype the ledger governs — parents only; children inherit and singles carry no matrix."""
	return frappe.get_all(
		"DocType",
		filters={"istable": 0, "issingle": 0},
		fields=["name", "module"],
		order_by="name",
	)


def executable_doctypes():
	"""Doctypes carrying a field whose stored value is EXECUTED — the R2 law, read off the live schema.

	Derived, never typed: an app upgrade that introduces a new executable field is covered on arrival.
	Custom Field is unioned in because a field added to another app's doctype is just as executable."""
	found = set()
	for table in ("DocField", "Custom Field"):
		key = "parent" if table == "DocField" else "dt"
		for row in frappe.get_all(
			table, filters={"fieldtype": ["in", list(ledger.EXECUTABLE_FIELDTYPES)]}, fields=[key]
		):
			found.add(row[key])
	return found


def narrowed_doctypes():
	"""Doctypes some app narrows at runtime, via a PQC or a has_permission hook.

	Without this the report lies by omission. `ToDo`, `File` and `Notification Settings` all carry a
	non-if_owner `All` write in their matrix, which reads as wide open — and all three are scoped by a
	frappe-core hook that the matrix cannot show. An unscoped grant is only a finding when NOTHING
	narrows it, so the two facts are reported side by side and neither is collapsed into the other."""
	hooks = frappe.get_hooks()
	return set(hooks.get("permission_query_conditions") or {}) | set(hooks.get("has_permission") or {})


def _row_count(doctype):
	"""Row count, or None when there is no table to count — a virtual doctype has none by design."""
	if frappe.get_meta(doctype).is_virtual:
		return None
	return frappe.db.count(doctype)


def deltas():
	"""Per parent doctype: what is enforced, what the ledger wants, and whether those differ.

	`declared` separates the two reasons a doctype can differ — a reviewed OPEN entry that has drifted,
	versus a doctype nobody has classified yet. They need opposite responses, so they are never merged."""
	out = []
	executable = executable_doctypes()
	narrowed = narrowed_doctypes()
	for dt in parent_doctypes():
		name = dt.name
		now, want = enforced_rows(name), ledger.rows_for(name)
		out.append(
			{
				"doctype": name,
				"module": dt.module,
				"declared": ledger.is_declared(name),
				"bucket": ledger.bucket_for(name),
				"roles_now": len(now),
				"roles_want": len(want),
				"open_to_all_now": ledger.grants_all_write(now),
				"narrowed": name in narrowed,
				"executable": name in executable,
				"changes": _normalise(now) != _normalise(want),
			}
		)
	return out


def _normalise(rows):
	"""Compare on the four flags plus if_owner, padded — a 4-tuple and a 5-tuple ending in 0 are equal."""
	return {role: tuple(perms[:5]) + (0,) * (5 - len(perms[:5])) for role, perms in rows.items()}


def undeclared_ranked():
	"""Undeclared doctypes worth a human's attention first, most-evidence first.

	Score is the count of TRUE signals, so it never implies a precision the evidence does not carry."""
	rows = [d for d in deltas() if not d["declared"]]
	for d in rows:
		d["rows"] = _row_count(d["doctype"])
		# An unscoped All grant only scores when nothing narrows it at runtime — see narrowed_doctypes.
		unscoped = d["open_to_all_now"] and not d["narrowed"]
		d["score"] = sum((d["executable"], unscoped, bool(d["rows"])))
	rows.sort(key=lambda d: (-d["score"], -(d["rows"] or 0), d["doctype"]))
	return rows


def parity_with_locked_matrix():
	"""Doctypes where the ledger and the live LOCKED_MATRIX disagree — must be empty before Phase 2.

	This is the proof that swapping the reader is a no-op: same doctypes, same rows, so the first
	enforcement run changes nothing and any later change is deliberate."""
	from tatva_connect.access.lockdown import LOCKED_MATRIX

	mismatched = []
	for name in set(LOCKED_MATRIX) | set(ledger.OPEN):
		want = _normalise(dict(LOCKED_MATRIX.get(name, {})))
		have = _normalise(ledger.rows_for(name)) if name in ledger.OPEN else {}
		if want != have:
			mismatched.append(name)
	return sorted(mismatched)


def report():
	"""Print the Phase 1 audit. Read-only; safe on any site, including production."""
	rows = deltas()
	declared = [d for d in rows if d["declared"]]
	undeclared = undeclared_ranked()
	open_now = [d for d in rows if d["open_to_all_now"]]

	print(f"\nparent doctypes        {len(rows)}")
	print(f"  declared (OPEN)      {len(declared)}")
	print(f"  undeclared -> DENIED {len(undeclared)}")
	print(f"  open to All/Guest    {len(open_now)}")
	print(f"  carry executable fld {len([d for d in rows if d['executable']])}")

	drift = parity_with_locked_matrix()
	print(f"\nLOCKED_MATRIX parity   {'OK' if not drift else 'DRIFT: ' + ', '.join(drift)}")

	print(
		"\ntop undeclared by evidence (exec = Code field, allw = All/Guest write, narr = a hook narrows it)"
	)
	print(f"  {'doctype':<40} {'module':<20} {'exec':>5} {'allw':>5} {'narr':>5} {'rows':>8}")
	for d in undeclared[:40]:
		print(
			f"  {d['doctype'][:40]:<40} {(d['module'] or '')[:20]:<20} "
			f"{'Y' if d['executable'] else '':>5} {'Y' if d['open_to_all_now'] else '':>5} "
			f"{'Y' if d['narrowed'] else '':>5} "
			f"{(d['rows'] if d['rows'] is not None else '-'):>8}"
		)
	print(f"\n  ... {max(0, len(undeclared) - 40)} more\n")
	return {"total": len(rows), "declared": len(declared), "undeclared": len(undeclared), "drift": drift}
