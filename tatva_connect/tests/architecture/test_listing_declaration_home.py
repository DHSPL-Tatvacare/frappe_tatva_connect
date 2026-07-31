# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Locks 2 and 3 — the fork's listing declarations are upstream's, and ours name the lead once.

LOCK 2. The fork has a backend of its own and it is frappe/crm v1.73.2 as shipped. We went the other way
once (fork commit `089c974`) and tailored `default_list_data()` in place on four doctypes, which put a
rule about what a resource's fields are outside the app that owns the rules. Those four returned to
upstream on 2026-07-31 and the declarations now live in `tatva_connect/list_engine/columns.py`, reached
through `override_doctype_class`.

This test is what stops that being quietly undone by the next person who finds editing the fork easier.
It hashes every `default_list_data` body the fork carries and compares it to the v1.73.2 checksum. A
checksum, deliberately, and not a copy of the bodies: restating upstream's declaration here would be the
second brain the rehoming removed. An unknown file fails too — a NEW declaration in the fork is the same
deviation as an edited one.

Deliberately narrow. It names `default_list_data` and not "any function in the fork": `crm/api/activities.py`,
`dashboard.py`, `whatsapp.py` and `crm_dashboard.py` all carry deliberate edits today and the grandfathered
`crm/lead_syncing/` exception exists, so a general assertion is a much larger decision and is out of scope
(plan §8). It skips with a clear message when the fork is not installed beside this app, so CI never goes
red for a missing sibling.

LOCK 3. The lead reference is keyed `reference_docname` and labelled `Lead` on all three child listing
surfaces — Task, Call Log and Note. Read off the controller through `get_controller`, which is what every
real consumer calls; the expected label is the only literal, and the three doctypes must agree.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.architecture.test_listing_declaration_home
"""

import ast
import hashlib
import pathlib
import unittest

import frappe
from frappe.model.document import get_controller
from frappe.tests.utils import FrappeTestCase

DECLARATION = "default_list_data"

# sha256 of `ast.get_source_segment` for every `default_list_data` frappe/crm v1.73.2 ships, taken at
# fork commit 3c0c3e9 (`docs: lean-fork manifest (forked frappe/crm @ v1.73.2)`). Path is fork-relative.
UPSTREAM_DECLARATION_DIGESTS = {
	"crm/fcrm/doctype/crm_call_log/crm_call_log.py": "238a07994699204b85ebaf12f7b4cde88e1d65534642123c0336ab4695899f78",
	"crm/fcrm/doctype/crm_deal/crm_deal.py": "2f338f9e8846134e23f889b4b13f50bce836bf3c3079d4cd974d7240c14399d8",
	"crm/fcrm/doctype/crm_lead/crm_lead.py": "1c3467b0727dc62d11e03bef02a4fb90e10e8b8b4fcfc0c5c99bd68d99ffa546",
	"crm/fcrm/doctype/crm_organization/crm_organization.py": "b56702189bf1296c651b0f6d4910ad034c183e25752e54aa87d07ec864b8377e",
	"crm/fcrm/doctype/crm_task/crm_task.py": "2f951816b397d6261d170f12bd9f7251f838c80a6065a36008da5391d947b504",
	"crm/fcrm/doctype/fcrm_note/fcrm_note.py": "0e1f112b5bace67aed2f0a38eba1a752d4bc0b3383ce0242702da14868c3a8b8",
	"crm/overrides/contact.py": "91da6831a3cb1c3bfbc29175768ef96617bb29950419f6d159bfcf8c72783ef2",
	"crm/overrides/email_template.py": "422274d0f8f33e566fcde8d5a2a0ac68b7a076bbec34444d39e1d084d13bbcb0",
}

LEAD_REFERENCE_KEY = "reference_docname"
LEAD_REFERENCE_LABEL = "Lead"
CHILD_LISTING_DOCTYPES = ("CRM Task", "CRM Call Log", "FCRM Note")


def _fork_root():
	"""The fork's package root, or None when it is not installed beside this app."""
	try:
		return pathlib.Path(frappe.get_app_path("crm")).parent
	except Exception:
		return None


def _declaration_digests(root):
	"""{fork-relative path: sha256 of the declaration's source} for every one the fork carries."""
	found = {}
	for py in (root / "crm").rglob("*.py"):
		src = py.read_text()
		if f"def {DECLARATION}" not in src:
			continue
		for node in ast.walk(ast.parse(src)):
			if isinstance(node, ast.FunctionDef) and node.name == DECLARATION:
				segment = ast.get_source_segment(src, node)
				found[str(py.relative_to(root))] = hashlib.sha256(segment.encode()).hexdigest()
	return found


class TestForkListingBackendIsUpstreams(FrappeTestCase):
	def setUp(self):
		self.root = _fork_root()
		if self.root is None:
			raise unittest.SkipTest("the crm fork is not installed beside this app; nothing to check")

	def test_no_declaration_in_the_fork_differs_from_upstream(self):
		found = _declaration_digests(self.root)
		self.assertEqual(
			sorted(found), sorted(UPSTREAM_DECLARATION_DIGESTS),
			"a default_list_data appeared in or vanished from the fork — a listing declaration is ours "
			"and belongs in tatva_connect/list_engine/columns.py, wired through override_doctype_class",
		)
		edited = [
			path for path, digest in found.items()
			if digest != UPSTREAM_DECLARATION_DIGESTS[path]
		]
		self.assertEqual(
			edited, [],
			"the fork's backend must stay frappe/crm v1.73.2: these declarations were edited in place "
			f"instead of being declared in tatva_connect/list_engine/columns.py — {edited}",
		)


class TestOneNameForTheLead(FrappeTestCase):
	def test_the_lead_reference_is_keyed_and_labelled_once(self):
		"""Lock 3 — read off the controller every consumer reads, never restated as a column list."""
		for doctype in CHILD_LISTING_DOCTYPES:
			with self.subTest(doctype=doctype):
				columns = get_controller(doctype).default_list_data()["columns"]
				lead = [c for c in columns if c.get("key") == LEAD_REFERENCE_KEY]
				self.assertEqual(len(lead), 1, f"{doctype} must declare exactly one lead column")
				self.assertEqual(lead[0]["label"], LEAD_REFERENCE_LABEL)
				self.assertEqual(lead[0]["type"], "Dynamic Link")
				self.assertEqual(lead[0]["options"], "reference_doctype")
