# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Which dashboard a person gets must be the same answer every time they open it.

Nobody on this site holds ONE role. A CRM Field Manager holds four, so matching a layout by role matches
several as a matter of course — and without a stated tie-break the dashboard somebody sees would depend
on row order, which is to say on nothing anyone chose. Four properties, and they are the whole contract
Phase 2 reads:

  * THE HIGHEST PRIORITY WINS when several roles grant a layout.
  * EQUAL PRIORITIES STILL RESOLVE THE SAME WAY, by role name, so a tie is stable rather than arbitrary.
  * A DISABLED LAYOUT NEVER WINS — that is what untick is for, and it must not merely be skipped in the
    UI while still beating a lower-priority row that would have worked.
  * NO MATCH IS `None`, not somebody else's dashboard. A rep must never be shown a manager's cards.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.dashboard.test_resolver
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.dashboard import declaration, resolver

CHART = declaration.CHART
LAYOUT = declaration.LAYOUT
PROBE_CHART = "probe_resolver_card"
PROBE_USER = "probe-resolver@tatvacare.test"
# Deliberately named so `role asc` puts "Alpha" first — the tie-break has to be readable in the test.
ALPHA = "Probe Dash Alpha"
BETA = "Probe Dash Beta"


class ResolverCase(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._clear()
		frappe.get_doc(
			{
				"doctype": CHART,
				"chart_name": PROBE_CHART,
				"label": "Probe Card",
				"chart_type": "number",
				"source_doctype": "CRM Lead",
				"aggregate": "COUNT",
				"date_field": "creation",
			}
		).insert(ignore_permissions=True)
		for role in (ALPHA, BETA):
			if not frappe.db.exists("Role", role):
				frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 0}).insert(
					ignore_permissions=True
				)
		user = frappe.get_doc(
			{"doctype": "User", "email": PROBE_USER, "first_name": "Probe", "send_welcome_email": 0}
		).insert(ignore_permissions=True)
		user.add_roles(ALPHA, BETA)

	def tearDown(self):
		self._clear()

	def _clear(self):
		frappe.db.delete(declaration.PLACEMENT_DOCTYPE, {"parenttype": LAYOUT})
		frappe.db.delete(LAYOUT, {"role": ["in", [ALPHA, BETA]]})
		frappe.db.delete("Has Role", {"role": ["in", [ALPHA, BETA]]})
		frappe.db.delete("User", {"name": PROBE_USER})
		frappe.db.delete("Role", {"name": ["in", [ALPHA, BETA]]})
		frappe.db.delete(CHART, {"chart_name": PROBE_CHART})
		self._forget()

	def _forget(self):
		"""The answer is request-cached, and one test process is one request."""
		setattr(frappe.local, "tatva_connect:dashboard_layout", {})

	def _seed_layout(self, role, priority, enabled=1):
		frappe.get_doc(
			{
				"doctype": LAYOUT,
				"role": role,
				"title": role,
				"enabled": enabled,
				"priority": priority,
				"charts": [{"chart": PROBE_CHART, "x": 0, "y": 0, "w": 4, "h": 3}],
				"exposed_filters": json.dumps([]),
			}
		).insert(ignore_permissions=True)
		self._forget()


class TestOneUserGetsOneDashboard(ResolverCase):
	def test_the_highest_priority_layout_wins(self):
		self._seed_layout(ALPHA, priority=1)
		self._seed_layout(BETA, priority=9)
		self.assertEqual(resolver.layout_for(PROBE_USER)["role"], BETA)

	def test_an_equal_priority_tie_resolves_by_role_name(self):
		self._seed_layout(ALPHA, priority=5)
		self._seed_layout(BETA, priority=5)
		self.assertEqual(resolver.layout_for(PROBE_USER)["role"], ALPHA)

	def test_a_disabled_layout_never_wins(self):
		self._seed_layout(ALPHA, priority=1)
		self._seed_layout(BETA, priority=9, enabled=0)
		self.assertEqual(resolver.layout_for(PROBE_USER)["role"], ALPHA)

	def test_no_matching_layout_answers_none(self):
		"""`None` is the contract Phase 2 renders as "no dashboard configured" — never a fallback layout."""
		self.assertIsNone(resolver.layout_for(PROBE_USER))

	def test_the_answer_carries_what_the_dashboard_is_made_of(self):
		self._seed_layout(BETA, priority=9)
		layout = resolver.layout_for(PROBE_USER)
		self.assertEqual(layout["charts"][0]["chart"], PROBE_CHART)
