# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from tatva_connect.automation import registry
from tatva_connect.automation.settings import is_enabled


class CRMTatvaAutomation(Document):
	# A row's name IS its key, so the shape is enforced here too — the registry is not the only writer.
	def validate(self):
		try:
			registry.assert_valid_key(self.automation_key or self.name)
		except ValueError as e:
			frappe.throw(str(e), title=_("Invalid automation key"))
		# The blocking half: a child cannot be armed above a dormant parent.
		# The parent is read from the REGISTRY, never from this row — the registry declares the hierarchy and
		# the `requires` column is only its projection, so a stale or edited row cannot buy a child its arming.
		# `is_enabled` walks the whole chain, so this refuses on ANY dormant ancestor, not just the nearest.
		# Fires on the ARMING itself, never on a refresh that leaves `enabled` alone: the seed re-saves every
		# row to update labels, and re-litigating a state it is not changing is what would fail a migrate.
		parent = registry.parent_of(self.automation_key or self.name)
		if self.enabled and self.has_value_changed("enabled") and parent and not is_enabled(parent):
			frappe.throw(
				_("{0} cannot be enabled while {1} is off. The automation it leans on is enabled first.").format(
					self.automation_key, parent
				),
				title=_("Dependency is off"),
			)

	# `enabled` is operator-owned — no controller logic forces or locks it. On a real flip we
	# only reconcile the infrastructure a toggle owns (its activator); deploy-time state is set
	# by automation.seed.reconcile_activations, so migrate/install are skipped here.
	def on_update(self):
		if frappe.flags.in_install or frappe.flags.in_migrate:
			return
		if not self.has_value_changed("enabled"):
			return
		if not self.enabled:
			self._switch_children_off()
		activator = registry.activator_for(self.name)
		if activator:
			frappe.get_attr(activator)(bool(self.enabled))

	# The cascading half: switching a parent off is NEVER refused, so it takes its children with it.
	# Each child is saved rather than written, so a child that owns infrastructure disarms its own.
	# Nothing cascades the other way — arming a parent arms nothing else.
	def _switch_children_off(self):
		# Children come from the REGISTRY for the same reason the parent does — a row that stopped
		# declaring its parent must not be able to stay armed by escaping the cascade.
		declared = [auto.key for auto in registry.AUTOMATIONS if auto.requires == self.name]
		children = frappe.get_all(
			"CRM Tatva Automation", filters={"name": ["in", declared], "enabled": 1}, pluck="name"
		) if declared else []
		for child in children:
			doc = frappe.get_doc("CRM Tatva Automation", child)
			doc.enabled = 0
			doc.save()
		if children:
			frappe.msgprint(
				_("Switched off with it, because each one leans on it: {0}.").format(", ".join(children)),
				title=_("Dependent automations switched off"),
				indicator="orange",
			)
