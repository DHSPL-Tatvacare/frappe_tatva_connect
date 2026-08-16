# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Publishing a dashboard to the open internet is a platform act, not an authoring one.

`is_public` on a dashboard makes it readable with no login, and the path that serves it sets
`insights_for_public_access`, which skips BOTH `apply_user_permissions` and `check_table_permission` —
so a published dashboard is the one surface the ledger does not reach.

Permlevel cannot hold it: `update_access` writes the field with `db_set`, which never runs the field
check. The gate is therefore the method, reached through the class override we already carry.

Turning it OFF is deliberately left to any author. Pulling an exposed dashboard down should never wait
for an administrator, and the fail-safe direction needs no permission.
"""
import frappe
from frappe import _

from tatva_connect.access import visibility


def assert_may_publish(dashboard, data):
	"""Refuse a request that turns `is_public` ON unless the caller is the platform tier."""
	if not data.get("is_public") or dashboard.is_public:
		return
	if visibility.is_privileged():  # the ONE spelling of Administrator-or-System-Manager
		return
	frappe.throw(
		_(
			"Publishing a dashboard makes it readable by anyone with the link, without signing in. "
			"Ask a System Manager to publish it. Sharing with people here, or with the whole "
			"organisation, needs no such approval."
		),
		frappe.PermissionError,
		title=_("Not published"),
	)
