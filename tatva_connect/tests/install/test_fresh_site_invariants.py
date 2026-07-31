# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""What a site MUST have after install + migrate — run it against a virgin site to see what prod gets.

`bench install-app` BASELINES patches.txt: it writes a Patch Log row for every patch and runs NONE of
them. So a patch is only ever a heal for an existing site, and anything a NEW site needs must live where
a fresh install actually runs it — the doctype JSON, a fixture, a seed, or `schema_setup._STEPS` /
`access.lockdown` on after_migrate. That rule was broken once and nobody saw it: hash naming reached
every existing site and no fresh one, so a new prod VM shipped the `naming_series` deadlock the patch
exists to kill. This is the check that would have caught it, and it is why it exists.

Every assertion here must hold on ANY site — dev, a fresh probe, prod after a deploy — so the suite is
the answer to "what did prod actually get?" rather than an argument about it. Operator choices (which
switches are ON, what secrets are filled) are deliberately NOT asserted: those are the operator's, not
the deploy's.

To see what a brand-new prod VM gets:

    bench new-site probe.localhost --db-root-password <pw> --admin-password <pw>
    for app in payments telephony frappe_whatsapp crm tatva_connect; do bench --site probe.localhost install-app $app; done
    bench --site probe.localhost migrate
    bench --site probe.localhost set-config allow_tests true
    bench --site probe.localhost run-tests --app tatva_connect --module tatva_connect.tests.install.test_fresh_site_invariants
    bench drop-site probe.localhost --db-root-password <pw>

Run it once more with the `migrate` line DELETED — that is `TestInstallAloneIsCorrect` below. Different
question: not "is a deployed site correct" but "is the INSTALL correct, or is it rescued by the migrate
that happens to follow". It was the second for months, and nothing said so.
"""
import json
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase


class TestFreshSiteInvariants(FrappeTestCase):
	def test_leads_and_deals_are_named_by_hash_not_a_counter(self):
		"""A naming_series name is minted from ONE tabSeries row whose lock is held to commit, so concurrent
		creates deadlock — 1 of 32 survived a 32-way burst, and the partner API turned that into 124 HTTP
		500s. The patch that fixed it never ran on a fresh site; schema_setup is what carries it now."""
		for doctype in ("CRM Lead", "CRM Deal"):
			autoname = frappe.db.get_value("DocType", doctype, "autoname")
			self.assertEqual(autoname, "hash", f"{doctype} is named `{autoname}` — a counter deadlocks under load")

	def test_a_rep_can_see_the_grain_but_only_a_manager_can_move_a_lead(self):
		"""Product Line and Group are permlevel 1, and the permlevel-1 grant is what makes that a LOCK rather
		than a blackout: with no level-1 read the fields vanish for everyone, which is why they were once
		quietly unlocked. Both halves must be present or the lock is off."""
		meta = frappe.get_meta("CRM Lead")
		for field in ("custom_vertical", "custom_group"):
			self.assertEqual(meta.get_field(field).permlevel, 1, f"{field} is not manager-gated")

		grants = {
			(p.role, p.read, p.write)
			for p in frappe.get_all(
				"Custom DocPerm",
				filters={"parent": "CRM Lead", "permlevel": 1},
				fields=["role", "read", "write"],
			)
		}
		self.assertIn(("Sales Manager", 1, 1), grants, "a manager cannot move a lead between businesses")
		self.assertIn(("Sales User", 1, 0), grants, "a rep cannot SEE their own lead's product line")

	def test_the_notification_catalog_is_registered_and_wired(self):
		"""Every catalog event needs its switch row, or `is_enabled` gates on a key that does not exist."""
		from tatva_connect.notifications import catalog

		switches = set(frappe.get_all("CRM Tatva Automation", pluck="name"))
		for event in catalog.all_events():
			self.assertIn(event.automation_key, switches, f"{event.key} has no switch to gate it")

	def test_the_notification_opt_in_column_is_event_key(self):
		"""The rename must have landed, and the dead column must be gone — a stale grain_key column let a
		re-run of the rename copy NULLs over every rep's opt-ins."""
		self.assertTrue(frappe.db.has_column("CRM Notification Subscription", "event_key"))
		self.assertFalse(frappe.db.has_column("CRM Notification Subscription", "grain_key"))

	def test_the_tray_accepts_the_notification_types_we_write(self):
		"""crm's tray `type` is a Select. A missed-call notify writes `Call`; without the option the insert
		throws INSIDE the telephony webhook and the call log is lost."""
		options = (frappe.get_meta("CRM Notification").get_field("type").options or "").split("\n")
		for kind in ("Call", "Lead"):
			self.assertIn(kind, options, f"the tray rejects a `{kind}` notification")

	def test_the_task_due_sweep_has_its_stamps(self):
		"""Without them the sweep cannot remember who it told, and a rep is notified every five minutes."""
		for column in ("custom_due_soon_notified_for", "custom_overdue_notified_for"):
			self.assertTrue(frappe.db.has_column("CRM Task", column), f"CRM Task is missing {column}")

	def test_every_workspace_tile_resolves(self):
		"""A shortcut block is looked up by LABEL; a miss renders an empty tile and the operator sees a
		blank page under a heading that tells them to click it."""
		ws = frappe.get_doc("Workspace", "Communications")
		labels = {s.label for s in ws.shortcuts}
		referenced = [
			b["data"].get("shortcut_name")
			for b in json.loads(ws.content)
			if b.get("type") == "shortcut"
		]
		dangling = [r for r in referenced if r not in labels]
		self.assertEqual(dangling, [], f"empty tiles on the Communications workspace: {dangling}")

	def test_the_provider_account_forms_can_hold_a_real_credential(self):
		"""Acefone's API token is 285 chars against Frappe's 140-char default on a Password field, so an
		operator simply cannot paste it until the cap is raised."""
		self.assertEqual(frappe.get_meta("CRM Telephony Account").get_field("api_token").length, 1000)
		self.assertTrue(frappe.db.exists("Custom Field", "WhatsApp Account-custom_section_credentials"))


class TestInstallAloneIsCorrect(FrappeTestCase):
	"""`install-app` alone must leave a correct site — no migrate, no rescue.

	`after_migrate` fires at ONE place, `migrate.py:202`. `install_app` fires `after_install`
	(`installer.py:332`), `sync_fixtures` (`:339`) and `after_sync` (`:343`), and never `after_migrate` —
	so until `after_sync` was wired, a fresh site got no index, no seed row and no lockdown, and was
	correct only because a migrate always happened to follow.

	Every assertion below reads the DECLARATION and asks whether the site matches it, so a new index, a
	new switch or a new section is covered the day it is declared and never needs listing here.
	"""

	def test_the_schema_steps_indexes_are_on_a_freshly_installed_site(self):
		"""`install-app` BASELINES patches.txt without running it, so `schema_setup` is these indexes' only
		road onto a new site. The first two were measured absent on a fresh install before `after_sync` was
		wired; the third was in `schema_setup._STEPS` with nothing asserting it had ever arrived."""
		from tatva_connect.patches import (
			add_call_log_reference_index,
			add_integration_request_index,
			add_task_due_state_index,
		)

		self.assertTrue(
			frappe.db.has_index("tabCRM Call Log", add_call_log_reference_index.INDEX),
			f"{add_call_log_reference_index.INDEX} is missing — schema_setup never ran on this site",
		)
		self.assertTrue(
			frappe.db.has_index(
				f"tab{add_integration_request_index.DOCTYPE}", add_integration_request_index.INDEX
			),
			f"{add_integration_request_index.INDEX} is missing — schema_setup never ran on this site",
		)
		# (status, due_date) on CRM Task — the pair every derived due-state predicate, board column and count
		# seeks on; the table's other composites lead with reference_docname and cannot serve it.
		for name, _columns in add_task_due_state_index._INDEXES:
			self.assertTrue(
				frappe.db.has_index(add_task_due_state_index._TABLE, name),
				f"{name} is missing from {add_task_due_state_index._TABLE} — schema_setup never ran on this "
				"site, and every Task Status filter reads the whole table",
			)

	def test_every_declared_automation_has_its_switch_row(self):
		"""`automation.seed.sync_catalog`. A key with no row is a gate on a switch that does not exist."""
		from tatva_connect.automation.registry import AUTOMATIONS

		rows = set(frappe.get_all("CRM Tatva Automation", pluck="name"))
		missing = sorted(auto.key for auto in AUTOMATIONS if auto.key not in rows)
		self.assertEqual(missing, [], f"{len(missing)} declared automations have no catalog row")

	def test_every_declared_lead_section_has_its_row(self):
		"""`partner_api.section_seed.ensure_rows` — and the sharpest probe of the two, because its
		skip-until-ready guard reads a fixture Table field. Run this chain BEFORE `sync_fixtures` and it
		seeds nothing at all, silently, which is exactly what `after_install` would have done."""
		from tatva_connect.partner_api.section_seed import _ROWS

		rows = set(frappe.get_all("CRM Lead Section", pluck="name"))
		missing = sorted(row["section_key"] for row in _ROWS if row["section_key"] not in rows)
		self.assertEqual(missing, [], f"{len(missing)} declared lead sections were never seeded")

	def test_the_locked_doctypes_are_locked_on_a_freshly_installed_site(self):
		"""`access.lockdown.apply`. Unapplied, the stock-open matrix stands and All/Guest keeps its grants."""
		from tatva_connect.access.lockdown import LOCKED_MATRIX, effective_all_guest_grants

		bad = [grant for doctype in LOCKED_MATRIX for grant in effective_all_guest_grants(doctype)]
		self.assertEqual(bad, [], f"{len(bad)} All/Guest grants survive on locked doctypes")


if __name__ == "__main__":
	unittest.main()
