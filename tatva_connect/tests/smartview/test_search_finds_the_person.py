# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A search finds the PERSON, whatever the view happens to display.

THE DEFECT. `_apply_search` ORed a LIKE across "every projected filterable field", so the searchable
surface was whichever columns the view was built with. A rep typing a patient's phone into a view that
showed stage and owner got `No matches` — the phone was never compared. The same shape had a second
face: a view whose columns were all unfilterable produced NO likes at all, and the search was then
dropped in silence and the whole list came back looking searched. One rule told the reader two different
lies about the same answer.

THE RULE. Identity is searched whatever the view shows — the doctype's own `get_search_fields` /
`get_title_field` plus the app's single declaration of what identifies a patient (`search.index.
IDENTIFIERS`) — and the compared set never leaves the driving row, so a search costs no join in the
rows query and none in the count.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_search_finds_the_person
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.smartview import api as smartview
from tatva_connect.tests.api import partner_fixture

# The view DISPLAYS this and nothing else — deliberately not an identity field, which is the whole point.
SHOWN = "lead:custom_substage"
PHONE_A = "+916100070001"
PHONE_B = "+916100070002"
NAME_A = "Searchable Asha"
NAME_B = "Searchable Bina"


class TestSearchFindsThePerson(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge()
		partner_fixture.mint_grain()
		# The identity fields must be IN the catalog, or they are outside the allowlist and no rule about
		# search could be proved. They are ticked here rather than relied on: adding any contract narrows
		# the universal floor to its own ticks, so a test that leaned on that floor would prove nothing.
		cls.contract = frappe.get_doc({
			"doctype": "CRM Lead API Mapping", "contract_name": "ZZ Search Contract", "enabled": 1,
			"is_internal": 1, "vertical": partner_fixture.VERTICAL, "crm_group": partner_fixture.GROUP,
			"allowed_fields": [{"field": SHOWN}, {"field": "lead:mobile_no"}, {"field": "lead:first_name"}],
		}).insert(ignore_permissions=True).name
		cls.asha = cls._lead(NAME_A, PHONE_A)
		cls.bina = cls._lead(NAME_B, PHONE_B)
		cls.view = frappe.get_doc({
			"doctype": "CRM Smart View", "label": "ZZ Search View", "base_object": "Lead",
			"is_standard": 1, "vertical": partner_fixture.VERTICAL, "group": partner_fixture.GROUP,
			"columns": frappe.as_json([SHOWN]),
		}).insert(ignore_permissions=True).name
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		for name in frappe.get_all("CRM Smart View", filters={"label": "ZZ Search View"}, pluck="name"):
			frappe.delete_doc("CRM Smart View", name, force=True, ignore_permissions=True)
		if frappe.db.exists("CRM Lead API Mapping", cls.contract):
			frappe.delete_doc("CRM Lead API Mapping", cls.contract, force=True, ignore_permissions=True)
		partner_fixture.teardown()
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _purge(cls):
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", "+91610007%"]}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)

	@classmethod
	def _lead(cls, first_name, phone):
		return frappe.get_doc({
			"doctype": "CRM Lead", "first_name": first_name, "mobile_no": phone, "status": "New",
			"custom_vertical": partner_fixture.VERTICAL, "custom_group": partner_fixture.GROUP,
		}).insert(ignore_permissions=True).name

	def _names(self, term):
		return {r["name"] for r in smartview.get_data(self.view, search=term, page_size=200)["rows"]}

	def test_a_phone_finds_its_lead_though_the_view_never_shows_a_phone(self):
		"""THE defect, stated as the user meets it: the view displays one stage column, and the rep types
		a phone number. RED on the old rule, which compared only what was projected."""
		self.assertEqual(self._names(PHONE_A), {self.asha})

	def test_a_name_finds_its_lead_though_the_view_never_shows_a_name(self):
		"""The title field is identity too — `lead_name` is what the doctype itself calls this row."""
		self.assertIn(self.bina, self._names("Searchable Bina"))

	def test_a_term_that_matches_nobody_returns_nobody(self):
		"""The second face of the same defect: with no comparable field the search used to vanish and the
		FULL list came back, which reads as "these all match" — worse than an empty screen."""
		self.assertEqual(self._names("zzz-no-such-patient-zzz"), set())

	def test_the_count_agrees_with_the_rows(self):
		"""The count and the rows must answer one question: they are two queries over two join sets, and
		the search used to be built into only one of them from the projected set."""
		data = smartview.get_data(self.view, search=PHONE_A, page_size=200)
		self.assertEqual(data["total"], len(data["rows"]))
