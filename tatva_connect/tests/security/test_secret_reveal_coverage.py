# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every Password field on a doctype this app owns is revealable, and every reveal is logged.

A saved Password field holds only asterisks, so the stock eye toggle reveals nothing and the field reads
as broken. Two secrets had already drifted out of the allowlist this way while sitting on forms where
every neighbouring secret worked, which is what this locks.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.security.test_secret_reveal_coverage
"""
import os

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api.account_secrets import REVEALABLE

# Doctypes this app owns; a Password field on anything else belongs to core or another app.
OWNED = (
	"WhatsApp Account",
	"CRM Telephony Account",
	"CRM Push Settings",
	"CRM Maps Settings",
	"CRM Facebook Settings",
	"Lead Sync Source",
	"Facebook Page",
)


def _drop_probe_rows():
	"""The reveal audit commits by design, so these rows outlive the test-case rollback and are removed here."""
	frappe.db.delete("Access Log", {"export_from": "CRM Facebook Settings", "method": "reveal:app_secret"})
	frappe.db.commit()


def _password_fields(doctype):
	"""Every Password field the live meta reports, so a Custom Field or Property Setter counts too."""
	return {f.fieldname for f in frappe.get_meta(doctype).fields if f.fieldtype == "Password"}


class TestSecretRevealCoverage(FrappeTestCase):
	def test_every_owned_password_field_is_revealable(self):
		missing = {}
		for doctype in OWNED:
			if not frappe.db.exists("DocType", doctype):
				continue
			gap = _password_fields(doctype) - REVEALABLE.get(doctype, set())
			if gap:
				missing[doctype] = sorted(gap)
		self.assertEqual(
			missing,
			{},
			f"Password fields with no reveal wiring: {missing}. Add them to REVEALABLE and to the "
			f"doctype's client script, or the eye silently reveals nothing.",
		)

	def test_allowlist_names_no_field_that_stopped_existing(self):
		stale = {}
		for doctype, fieldnames in REVEALABLE.items():
			if not frappe.db.exists("DocType", doctype):
				continue
			gap = set(fieldnames) - _password_fields(doctype)
			if gap:
				stale[doctype] = sorted(gap)
		self.assertEqual(stale, {}, f"REVEALABLE names fields that are no longer Password fields: {stale}")

	def test_every_revealable_doctype_wires_the_helper(self):
		"""The allowlist alone reveals nothing: the form must call the shared helper for the eye to work."""
		app = frappe.get_app_path("tatva_connect")
		wired = set()
		for root, dirs, files in os.walk(app):
			dirs[:] = [d for d in dirs if d not in ("__pycache__", "node_modules", "dist")]
			for filename in files:
				if not filename.endswith(".js"):
					continue
				with open(os.path.join(root, filename)) as handle:
					body = handle.read()
				if "tatva_enable_secret_reveal" not in body:
					continue
				for doctype in REVEALABLE:
					if f"'{doctype}'" in body or f'"{doctype}"' in body:
						wired.add(doctype)
		self.assertEqual(
			sorted(set(REVEALABLE) - wired),
			[],
			"These doctypes are in REVEALABLE but no client script calls tatva_enable_secret_reveal for them.",
		)

	def test_reveal_writes_an_access_log_row(self):
		"""Who read which secret and when has to be answerable afterwards."""
		from tatva_connect.api.account_secrets import reveal

		# The stored secret is never written to: the reveal now commits, and a probe value would stick.
		before = frappe.db.count("Access Log", {"export_from": "CRM Facebook Settings"})
		reveal("CRM Facebook Settings", "CRM Facebook Settings", "app_secret")
		after = frappe.db.count("Access Log", {"export_from": "CRM Facebook Settings"})
		self.assertEqual(after, before + 1, "A reveal left no Access Log row.")
		self.addCleanup(_drop_probe_rows)

	def test_the_access_log_row_is_durable_outside_the_test_branch(self):
		"""The audit trail is what justifies revealing these fields at all, so it is not allowed to be
		best-effort. Core's `make_access_log` inserts for real only when `frappe.in_test`; in production it
		calls `deferred_insert()`, which parks the row in Redis — no persistence, an LRU eviction policy,
		and `creation`/`owner` re-stamped at flush. The reveal no longer takes that path at all, and this
		proves the row is really on disk by rolling the transaction back underneath it and reading it again.
		A row that only existed because `frappe.in_test` chose the other branch would not survive."""
		from tatva_connect.api.account_secrets import reveal

		reveal("CRM Facebook Settings", "CRM Facebook Settings", "app_secret")
		# Anything not committed dies here; a row that survives is really in the table.
		frappe.db.rollback()
		self.addCleanup(_drop_probe_rows)

		rows = frappe.get_all(
			"Access Log",
			filters={"export_from": "CRM Facebook Settings", "method": "reveal:app_secret"},
			fields=["user", "reference_document"],
		)
		self.assertTrue(rows, "the reveal audit row did not survive a rollback, so it is not durable")
		self.assertEqual(rows[-1].user, frappe.session.user, "the row must name the caller, not the flusher")
		self.assertEqual(rows[-1].reference_document, "CRM Facebook Settings")

	def test_a_field_outside_the_allowlist_is_refused(self):
		from tatva_connect.api.account_secrets import reveal

		with self.assertRaises(frappe.ValidationError):
			reveal("User", "Administrator", "api_secret")
