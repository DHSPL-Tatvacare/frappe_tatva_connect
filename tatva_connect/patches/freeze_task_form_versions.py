"""Freeze every Task Form as its first version and give it a lifecycle state, so nothing a rep sees changes."""
import frappe

from tatva_connect.authoring import lifecycle
from tatva_connect.taxonomy import form_versions


def execute():
	for name in frappe.get_all("CRM Task Type", pluck="name"):
		if frappe.db.exists(form_versions.DOCTYPE, {"task_type": name}):
			continue
		form = frappe.get_doc("CRM Task Type", name)
		form_versions.ensure_version(form)
		state = lifecycle.ACTIVE if form.enabled else lifecycle.SUSPENDED
		frappe.db.set_value("CRM Task Type", name, "lifecycle_state", state, update_modified=False)
