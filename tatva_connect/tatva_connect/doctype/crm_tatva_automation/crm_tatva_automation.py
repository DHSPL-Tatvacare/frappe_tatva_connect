# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from tatva_connect.automation import registry


class CRMTatvaAutomation(Document):
	# A row's name IS its key, so the shape is enforced here too — the registry is not the only writer.
	def validate(self):
		try:
			registry.assert_valid_key(self.automation_key or self.name)
		except ValueError as e:
			frappe.throw(str(e), title=_("Invalid automation key"))

	# `enabled` is operator-owned — no controller logic forces or locks it. On a real flip we
	# only reconcile the infrastructure a toggle owns (its activator); deploy-time state is set
	# by automation.seed.reconcile_activations, so migrate/install are skipped here.
	def on_update(self):
		if frappe.flags.in_install or frappe.flags.in_migrate:
			return
		if not self.has_value_changed("enabled"):
			return
		activator = registry.activator_for(self.name)
		if activator:
			frappe.get_attr(activator)(bool(self.enabled))
