# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A saved Insights Team tells every worker to drop its cached grants, through the channel each worker listens on."""
import time

import frappe
from frappe.tests import IntegrationTestCase

CHANNEL = "clear_persistent_cache"


class TestInsightsTeamChangeReachesEveryWorker(IntegrationTestCase):
	def test_a_team_save_broadcasts_after_commit(self):
		listener = frappe.cache.pubsub()
		listener.subscribe(CHANNEL)
		listener.get_message(timeout=1)  # the subscribe confirmation
		try:
			frappe.get_doc({"doctype": "Insights Team", "team_name": "zz-broadcast-team"}).insert(ignore_permissions=True)
			self.assertIsNone(listener.get_message(timeout=0.5), "broadcast went out before commit")
			frappe.db.after_commit.run()
			deadline, seen = time.monotonic() + 3, None
			while not seen and time.monotonic() < deadline:
				seen = listener.get_message(timeout=0.5)
			self.assertIsNotNone(seen, "a team save sent no cache broadcast")
			payload = frappe.parse_json(seen["data"].decode())
			self.assertEqual((payload["doctype"], payload["site"]), (None, frappe.local.site))
		finally:
			listener.close()
