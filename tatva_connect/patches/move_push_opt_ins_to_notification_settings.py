"""CRM Notification Preference retires with its Subscription rows; the opt-in carry was archived in .archive/notify-one-seam-2026-10-19 with the push ticks it filled."""
from tatva_connect.patches import _desk, _schema

PREFERENCE = "CRM Notification Preference"
SUBSCRIPTION = "CRM Notification Subscription"


def execute():
	for doctype in (SUBSCRIPTION, PREFERENCE):  # child before parent
		_schema.drop_doctype(doctype)
	_desk.reimport("workspace_sidebar", "communications.json")
