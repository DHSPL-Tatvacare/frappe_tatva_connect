# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Each app's Docs opens the wiki space Tatva Platform Settings names for it, and nothing when it names none.

Run:
    bench --site dev.localhost run-tests --app tatva_connect --module tatva_connect.tests.access.test_app_documentation
"""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.api.help_center import space, tree


def _space(route, published=1):
	return frappe.get_doc({"doctype": "Wiki Space", "space_name": route.title(), "route": route, "is_published": published}).insert()


def _settings(*rows):
	settings = frappe.get_single("Tatva Platform Settings")
	settings.set("app_documentation", [{"app": app, "wiki_space": wiki_space} for app, wiki_space in rows])
	return settings


class TestAppDocumentation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def test_each_app_opens_its_own_space_and_an_unnamed_app_opens_none(self):
		hd, crm = _space("zz-hd-docs"), _space("zz-crm-docs")
		_settings(("helpdesk", hd.name), ("crm", crm.name)).save()
		self.assertEqual(space("helpdesk")["url"], "/zz-hd-docs")
		self.assertEqual([s["route"] for s in tree("crm")["spaces"]], ["zz-crm-docs"])
		self.assertIsNone(space("lms"))

	def test_settings_refuse_an_unknown_app_a_second_row_and_an_unpublished_space(self):
		published, draft = _space("zz-ok-docs"), _space("zz-draft-docs", published=0)
		for rows in ((("not_an_app", published.name),),
		             (("helpdesk", published.name), ("helpdesk", published.name)),
		             (("helpdesk", draft.name),)):
			with self.subTest(rows=rows), self.assertRaises(frappe.ValidationError):
				_settings(*rows).save()
