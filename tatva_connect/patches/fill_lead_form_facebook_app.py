"""Fill each stored Facebook lead form's Facebook App from its Page, since a stored form is never saved again."""
import frappe
from frappe.utils.fixtures import sync_fixtures


def execute():
	if not frappe.get_meta("Facebook Lead Form").has_field("facebook_app"):
		sync_fixtures("tatva_connect")  # the field is a fixture, and fixtures sync after patches
	for page, app in frappe.get_all("Facebook Page", filters={"facebook_app": ["is", "set"]}, fields=["name", "facebook_app"], as_list=True):
		frappe.db.set_value("Facebook Lead Form", {"page": page}, "facebook_app", app, update_modified=False)
	frappe.db.commit()
