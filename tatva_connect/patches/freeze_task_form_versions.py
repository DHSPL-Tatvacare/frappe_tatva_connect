"""Every Task Form is frozen as its first version and given its lifecycle state, so V2 changes nothing a rep sees.

Frozen exactly as it stands, which is the form every existing task has been read with; an enabled form becomes Active and a
disabled one Suspended. No task row is written: an unstamped task already answered reads the first version
(`form_versions.version_of`). Idempotent: a form that already has a version is left alone."""
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
