# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Agent Manager is a helpdesk role, never a site administrator.

Native `update_agent_role` appends `System Manager` beside `Agent Manager` (helpdesk hd_agent.py:59), and
the endpoint is callable by any Agent Manager — so one helpdesk promotion hands over the whole site, and
any manager can hand it to anyone. Unlike the wrappers in `native_guards.py`, this REPLACES the native
method rather than gating it: the native body is the defect, so there is nothing to delegate to.

Same signature, same `only_for` gate, same demotion. Two differences: the manager role stacks on `Agent`
the way `Sales Manager` stacks on `Sales User`, and `System Manager` is neither granted nor revoked —
revoking it would strip a real administrator who also happens to work tickets.

Existing Agent Managers keep the System Manager they were already given; this changes promotions from
here on, not history.
"""
import frappe


@frappe.whitelist()
def update_agent_role(user: str, new_role: str):
	"""Promote or demote a helpdesk agent, without ever touching System Manager."""
	frappe.only_for(("Agent Manager", "System Manager"))

	user_doc = frappe.get_doc("User", user)

	if new_role == "Manager":
		user_doc.append_roles("Agent Manager", "Agent")  # a manager works tickets too, so the role stacks
	if new_role == "Agent":
		user_doc.append_roles("Agent")
		if "Agent Manager" in frappe.get_roles(user_doc.name):
			user_doc.remove_roles("Agent Manager")

	# authz-ok: tier-b — gated by frappe.only_for above; User is Tier 0 and a manager holds no write on it.
	user_doc.save(ignore_permissions=True)
