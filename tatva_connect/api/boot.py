# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""What the server tells the browser ONCE, at page load, before a single request is made.

`crm/www/crm.py` builds a dict and `crm.html:203-204` writes every key of it onto `window`, which is how
the app already receives its timezone, its system defaults and its translations. This module adds to that
bag from `tatva_connect`, through frappe's own `update_website_context` hook — the fork keeps no backend
logic and no line of `crm/www/crm.py` is touched.

WHY THERE HAS TO BE A KEY AT ALL. frappe-ui resources are cached with NO expiry and mirrored to
IndexedDB (`resources.js` — deliberate: stale-while-revalidate plus explicit invalidation, never a TTL).
The five field menus are such resources. So a rep who has opened a list page once holds that page's field
list forever, and a derived field an operator authored this morning would never be offered to them again
— not after a reload, not after a deploy, not ever, because nothing would change the key they cached it
under. `derived_field_version` is that key: it moves whenever any enabled declaration does, so the next
page load asks for a cache entry nobody has, the menus refetch exactly once, and "live on Save" is true
for a rep who was already logged in. Without it the promise is true only on the server.

TWO DOORS, ONE ANSWER. The page render is the door in production; `get_context_for_dev` is the door when
the frontend runs under vite. Both are answered by `keys()` and neither restates it.

Plan: docs/plans/tasks-ui/2026-07-31-derived-field-head.md §5
"""

import frappe

from tatva_connect.list_engine import derived


def keys():
	"""Everything this app adds to the boot bag. One read of a cached value, no query on a warm cache."""
	return {"derived_field_version": derived.declaration_version()}


def website_context(context):
	"""`update_website_context` hook — runs on every template page, adds nothing to pages without a boot.

	Only the app shell builds a `boot`, so this is inert for every website page on the site and cannot
	change one by accident."""
	boot = context.get("boot")
	if isinstance(boot, dict):
		boot.update(keys())


@frappe.whitelist(methods=["POST"], allow_guest=True)  # guest-ok: mirrors native allow_guest (the vite boot runs before a session exists); the developer_mode gate below refuses every caller, guest included, off a dev bench
def get_context_for_dev():
	"""The vite door. Native's own answer plus ours — the dev bundle reads the same `window` keys the
	rendered page does, so a version that only reached production would be a defect nobody saw locally.

	THE GATE IS OURS AND IT IS FIRST. Native gates on `developer_mode` too, but an override that inherits
	`allow_guest` and delegates has published a public endpoint whose only protection is a function it
	happens to call. It is restated here, before any work, and raised as a `PermissionError` so what
	refuses an anonymous caller is visible at the door rather than one import away."""
	if not frappe.conf.developer_mode:
		frappe.throw(frappe._("This method is only meant for developer mode"), frappe.PermissionError)

	from crm.www.crm import get_context_for_dev as _native

	return {**_native(), **keys()}
