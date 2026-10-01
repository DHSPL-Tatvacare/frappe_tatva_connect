"""Push opt-ins move onto each user's own Notification Settings row, and CRM Notification Preference retires with its Subscription rows."""
import frappe
from frappe.desk.doctype.notification_settings.notification_settings import create_notification_settings
from frappe.utils.fixtures import sync_fixtures

from tatva_connect.notifications import catalog
from tatva_connect.patches import _desk, _schema

PREFERENCE = "CRM Notification Preference"
SUBSCRIPTION = "CRM Notification Subscription"


def execute():
	if frappe.db.table_exists(SUBSCRIPTION):
		sync_fixtures("tatva_connect")  # the push fields are fixtures, and fixtures sync after patches
		_carry_opt_ins()
	for doctype in (SUBSCRIPTION, PREFERENCE):  # child before parent
		_schema.drop_doctype(doctype)
	_desk.reimport("workspace_sidebar", "communications.json")


def _carry_opt_ins():
	"""Each enabled opt-in becomes that user's checkbox; one for an event no longer in the catalog retires with the table."""
	fields = {e.key: e.field for e in catalog.all_events()}
	owner = dict(frappe.get_all(PREFERENCE, fields=["name", "user"], as_list=True))
	for row in frappe.get_all(SUBSCRIPTION, filters={"parenttype": PREFERENCE, "enabled": 1}, fields=["parent", "event_key"]):
		user, field = owner.get(row.parent), fields.get(row.event_key)
		if not (user and field and frappe.db.exists("User", user)):
			continue
		if not frappe.db.exists(catalog.USER_SETTINGS, user):
			create_notification_settings(user)
		frappe.db.set_value(catalog.USER_SETTINGS, user, {field: 1, catalog.USER_PUSH_MASTER: 1}, update_modified=False)
