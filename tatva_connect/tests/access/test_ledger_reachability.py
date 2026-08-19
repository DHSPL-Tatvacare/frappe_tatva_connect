# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A granted doctype must be usable by the roles it was granted to.

Two sweeps over the declaration: a client script may only read what its own form's roles can read, and
a picker must be readable by whoever can write the field. `CRM Lead Section` failed the first — it was
admin-only while the intake authoring screen listed it through a call that checks read.
"""
import os
import re

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import client_scripts_seed
from tatva_connect.access import ledger

# Named, not quietly filtered: these apps ship their own screens and gating. Delete a line to sweep them — the failures are real.
_NOT_SWEPT_YET = ("LMS ", "Course ", "Wiki ")

# The client-side reads that check a permission. `frappe.call` is absent — a whitelisted method carries its own gate.
_CHECKED_READ = re.compile(
	r"""frappe\.(?:db|client)\.(?:get_list|get_value|get_doc|get_count)\s*\(\s*['"]([^'"]+)['"]"""
)


def _readers(doctype):
	"""The roles the ledger lets READ this doctype — index 0 of its tuple is `read` (POLICY §4)."""
	return {role for role, perms in ledger.rows_for(doctype).items() if perms[0]}


def _writers(doctype):
	"""The roles that may CHANGE it — read alone shows a resolved title, never a picker."""
	return {role for role, perms in ledger.rows_for(doctype).items() if perms[1] or perms[2]}


class TestLedgerReachability(FrappeTestCase):
	def test_a_client_script_only_reads_what_its_own_form_may_read(self):
		app = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
		unreachable = []
		for name, dt, _view, rel in client_scripts_seed.SCRIPTS:
			path = os.path.join(app, rel)
			if not os.path.exists(path) or not ledger.is_declared(dt):
				continue
			holders = _readers(dt)
			with open(path, encoding="utf-8") as f:
				source = f.read()
			for target in set(_CHECKED_READ.findall(source)):
				if target == dt or not ledger.is_declared(target):
					continue
				denied = holders - _readers(target)
				if denied:
					unreachable.append(f"{name} ({dt}) reads {target!r}, denied to: {', '.join(sorted(denied))}")
		self.assertEqual(
			unreachable,
			[],
			"A Desk script reads a doctype its own form's roles may not. Either widen the target in the "
			"ledger, or read it through a whitelisted method that carries its own gate:\n  "
			+ "\n  ".join(unreachable),
		)

	def test_a_granted_doctypes_pickers_are_readable_by_the_same_roles(self):
		empty_pickers = []
		for dt in sorted(ledger.OPEN):
			if dt.startswith(_NOT_SWEPT_YET) or not frappe.db.exists("DocType", dt):
				continue
			holders = _writers(dt)
			if not holders:
				continue
			for df in frappe.get_meta(dt).get("fields", {"fieldtype": ("in", ("Link", "Table", "Table MultiSelect"))}):
				target = df.options
				# A child table carries no permission rows of its own — frappe reads it through this parent.
				if not target or target == dt or not ledger.is_declared(target):
					continue
				# A field nobody can type into offers no picker; the app fills it and the role just reads it back.
				if df.read_only or df.hidden or frappe.get_meta(target).istable:
					continue
				denied = holders - _readers(target)
				if denied:
					empty_pickers.append(f"{dt}.{df.fieldname} -> {target}, denied to: {', '.join(sorted(denied))}")
		self.assertEqual(
			empty_pickers,
			[],
			"A field offers a picker over a doctype its own form's roles may not read — the list renders "
			"empty for exactly the people who were given the form:\n  " + "\n  ".join(empty_pickers),
		)
