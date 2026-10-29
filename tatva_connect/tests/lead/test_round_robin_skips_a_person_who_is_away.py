# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A Round Robin pool that requires check-in skips a person who is auto-away, for leads and for tickets alike."""

import time

import frappe
from frappe.tests import set_user

from tatva_connect import presence
from tatva_connect.api import checkin as checkin_api
from tatva_connect.lead import checkin
from tatva_connect.tests.workflow_engine import fixtures as fx
from tatva_connect.workflow_engine.tests.fixtures import make_pool

SETTINGS = "Tatva Platform Settings"


class TestRoundRobinSkipsAPersonWhoIsAway(fx.PoolTestCase):
	@classmethod
	def setUpClass(cls):
		cls.addClassCleanup(frappe.clear_document_cache, SETTINGS)
		super().setUpClass()
		settings = frappe.get_single(SETTINGS)
		settings.auto_away_minutes = 5
		settings.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, rolled back with the class
		cls.here, cls.gone = fx.rep("rr-here"), fx.rep("rr-gone")
		for user in (cls.here, cls.gone):
			with set_user(user):
				checkin_api.set_status(checkin.ACTIVE)
		presence._heard(cls.here, time.time())  # only `here` touched the app; `gone` never did, so reads auto-away
		members = [{"user": cls.here}, {"user": cls.gone}]
		cls.lead_pool = make_pool(members, rule="Round Robin", assigned_by_workflow=1, require_checkin=1)
		cls.ticket_pool = make_pool(members, rule="Round Robin", document_type="HD Ticket", require_checkin=1)

	def test_a_lead_never_reaches_the_person_who_is_away(self):
		self.assertEqual({fx.distribute(self.lead_pool, lead)[1] for lead in fx.leads(3)}, {self.here})

	def test_a_ticket_never_reaches_the_person_who_is_away(self):
		picked = set()
		for _ in range(3):
			user = self.ticket_pool.get_user({"doctype": "HD Ticket"})
			self.ticket_pool.db_set("last_user", user)  # as core's do_assignment records each pick, so the turn moves
			picked.add(user)
		self.assertEqual(picked, {self.here})
