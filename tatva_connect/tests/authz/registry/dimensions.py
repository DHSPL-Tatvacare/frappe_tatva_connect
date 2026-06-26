# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The tuple dimensions (the "who/what" axes) — data, for documentation + coverage auditing.

Cases in cases.py are concrete points in this space. The catalog lets a coverage check assert that
every dimension value appears in at least one case (so no axis is silently untested).
"""
DIMENSIONS = {
	"grain_held": ["none", "one", "multiple", "wildcard_axis", "all"],
	"grain_source": ["assignment_rule", "partner_mapping", "reports_to_rollup", "none"],
	"hierarchy": ["leaf", "manager_with_reports", "no_hierarchy", "deep_chain"],
	"role": ["sales_user", "sales_manager", "whatsapp_user", "whatsapp_admin",
	         "partner", "no_role", "system_manager", "guest", "combo", "cross_app_junk"],
	"user_permission": ["none", "fenced", "fenced_ignore_field"],
	"permlevel": ["field_l0", "field_l1_with_access", "field_l1_without_access"],
	"doctype_perms": ["locked", "stock", "drifted"],
	"target_row": ["in_grain", "out_of_grain", "same_program_diff_vertical",
	               "orphan_parent", "wildcard"],
	"action": ["read", "write", "create", "delete", "field_read"],
	"surface": ["list", "doc", "field", "bypass_write", "method", "http", "ui"],
}


def values(dimension):
	return DIMENSIONS[dimension]
