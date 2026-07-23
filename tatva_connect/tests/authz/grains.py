# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The 5 canonical test grains (Product Line / Group / Program) + master-existence validation.

Chosen so every isolation boundary has population on both sides:
  #1/#2 share vertical+group (Goodflip-Care · Anaya), differ on program  -> program-axis isolation
  #3/#4 share vertical+group (Tatvapractice · India), differ on program
  #4/#5 share the PROGRAM NAME "Inside-Sales" but differ on vertical+group -> the escalation trap:
        if grain matching ever keys on program alone, #4's user would see #5's leads.

Grain axes are Link fields to real masters (CRM Vertical / CRM Group / CRM Program), so the
generator can only LINK to existing master rows — never auto-create them (constitution invariant
4/10). assert_masters_exist() fails loud and tells the operator which master to seed.
"""
import frappe

# Which master doctype backs each grain axis (confirmed: CRM Lead.custom_* are Links to these).
_AXIS_MASTER = {"vertical": "CRM Vertical", "group": "CRM Group", "program": "CRM Program"}


def grain_key(vertical, group, program):
	return "{}::{}::{}".format(vertical or "", group or "", program or "")


def _g(vertical, group, program):
	return {
		"key": grain_key(vertical, group, program),
		"vertical": vertical,
		"group": group,
		"program": program,
	}


GRAINS = [
	_g("Goodflip-Care", "Anaya", "Nivolumab"),
	_g("Goodflip-Care", "Anaya", "Tukavo"),
	_g("Tatvapractice", "India", "Field-Sales"),
	_g("Tatvapractice", "India", "Inside-Sales"),
	_g("Goodflip", "India", "Inside-Sales"),
]

WILDCARD = _g("", "", "")  # blank axes = universal (visible to everyone) — for over-show checks


def missing_masters():
	"""Return [(axis_doctype, value), ...] for any grain axis value with no master row.

	Distinct values only; blank axes are skipped (a blank axis is the legitimate wildcard).
	"""
	missing = []
	seen = set()
	for g in GRAINS:
		for axis, doctype in _AXIS_MASTER.items():
			value = g[axis]
			if not value or (doctype, value) in seen:
				continue
			seen.add((doctype, value))
			if not frappe.db.exists(doctype, value):
				missing.append((doctype, value))
	return missing


def assert_masters_exist():
	"""Fail loud if any canonical grain references a master that isn't seeded. Never auto-create."""
	missing = missing_masters()
	if missing:
		rows = ", ".join(f"{dt}='{v}'" for dt, v in missing)
		frappe.throw(
			"authz grains reference masters that do not exist on this site — seed them first "
			f"(masters are curated, never auto-created): {rows}"
		)
