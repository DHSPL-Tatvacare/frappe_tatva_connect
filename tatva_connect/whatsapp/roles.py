# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""WhatsApp capability roles — the ONE brain for "who may use WhatsApp".

WhatsApp is a CAPABILITY, granted by these roles and nothing else; Sales membership is
irrelevant to it. A capability role grants NO lead access — lead visibility stays with the
user's functional role (Sales User, helpdesk, ...). The three gates compose (AND):

    capability role  x  can read THIS lead (functional role)  x  lead has a WATI route (grain)

Every WhatsApp surface reads its policy from here, never from a second list:
  - the fork's validate_access() reads CAPABILITY_ROLES via the `whatsapp_capability_roles` hook;
  - lockdown.py grants the upstream WhatsApp doctype perms from these same constants;
  - the all-accounts template sweep reads ADMIN_ROLES;
  - the SPA tab reads the same role names from frappe.boot.user.roles.

The two roles ship as a fixture (Role Manager + operator-assignable) but are assigned to nobody —
the feature is dormant until an operator grants them.
"""

WHATSAPP_USER = "WhatsApp User"    # use: tab, send templates/messages, sync own account
WHATSAPP_ADMIN = "WhatsApp Admin"  # configure: accounts, routing, all-account sweep (a superset of User)

# May use WhatsApp at all. System Manager is the inherent superuser backstop.
CAPABILITY_ROLES = [WHATSAPP_USER, WHATSAPP_ADMIN, "System Manager"]

# May configure WhatsApp. System Manager kept as the config backstop so config is never orphaned.
ADMIN_ROLES = [WHATSAPP_ADMIN, "System Manager"]
