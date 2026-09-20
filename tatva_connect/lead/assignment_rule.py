# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Override of core `Assignment Rule`: gate CRM Lead assignment on grain.

A grain-tagged rule may only fire on a lead whose grain matches every SET axis; a blank axis is a wildcard.
Stock for non-CRM-Lead rules and for rules with no grain set. Vertical and group are mandatory on a CRM Lead
rule, `grain_program` is not — one Anaya rule must serve Sigrima, Ujvira, Tukavo and Nivolumab."""

import frappe
from frappe.automation.doctype.assignment_rule.assignment_rule import AssignmentRule
from frappe.utils import cint, today

CREDIT_WEIGHTED = "Credit Weighted"


class TatvaAssignmentRule(AssignmentRule):
	def apply_assign(self, doc):
		# `apply_assign` is core's only assigning step on a save; a workflow pool is drawn from by its Distribute node instead.
		if self.get("assigned_by_workflow"):
			return False
		if self.document_type == "CRM Lead" and not self._lead_grain_matches(doc):
			return False
		return super().apply_assign(doc)

	def get_user(self, doc):
		if self.rule == CREDIT_WEIGHTED:
			return self.get_credit_weighted_user(doc)
		return super().get_user(doc)

	def get_credit_weighted_user(self, doc):
		"""Smooth weighted round robin over the members who can take this lead now, locked on the rule row like core's `current_index`.

		Eligibility is settled BEFORE the lock: entitlement and daily cap are a read per member, and holding the
		pool through them is what turned a draw into a queue. The lock covers the counter alone."""
		from tatva_connect.taxonomy import grain

		members = frappe.get_all(
			"Assignment Rule User",
			filters={"parenttype": "Assignment Rule", "parent": self.name, "parentfield": "weighted_users"},
			fields=["user", "weight", "daily_cap", "paused"],
			order_by="idx asc",
		)
		axes = tuple(doc.get(column) or "" for column in grain.columns(self.document_type))
		eligible = [m for m in members if self._can_take_a_lead(m, axes)]
		if not eligible:
			return None

		stored = frappe.parse_json(
			frappe.db.get_value("Assignment Rule", self.name, "credits", for_update=True) or "{}"
		)
		# A removed member's credit is dropped, so a re-added user starts level.
		credits = {m.user: cint(stored.get(m.user)) for m in members}
		for member in eligible:
			credits[member.user] += member.weight or 1
		winner = max(eligible, key=lambda m: credits[m.user])
		credits[winner.user] -= sum(m.weight or 1 for m in eligible)
		frappe.db.set_value(
			"Assignment Rule", self.name, "credits", frappe.as_json(credits), update_modified=False
		)
		return winner.user

	def _can_take_a_lead(self, member, axes):
		from tatva_connect.access import entitlement

		# Cancelled ToDos count toward the cap: a lead handed on later in the day was still received.
		return (
			not member.paused
			and frappe.get_cached_value("User", member.user, "enabled")
			and (not any(axes) or entitlement.grain_entitled(axes, user=member.user))
			and not (
				member.daily_cap
				and frappe.db.count(
					"ToDo",
					{"allocated_to": member.user, "assignment_rule": self.name, "creation": [">=", today()]},
				) >= member.daily_cap
			)
		)

	def _lead_grain_matches(self, doc):
		for rule_field, lead_field in (
			("grain_vertical", "custom_vertical"),
			("grain_group", "custom_group"),
			("grain_program", "custom_current_program"),
		):
			rule_value = self.get(rule_field)
			if rule_value and doc.get(lead_field) != rule_value:
				return False
		return True
