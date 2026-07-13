"""Drop the wiki app's demo space on every migrate (idempotent, isolated).

`wiki/install.py:after_install` seeds a Wiki Space named "Wiki" at route "docs", holding one page,
"Welcome to Frappe Wiki", so a fresh install is not an empty screen. It is a demo, it is not ours, and
it collides with us: /docs is the API reference site, served by nginx out of the sites volume. The demo
space carries switcher_order 0, so it sorts ABOVE the handbook and the API reference in wiki's own space
switcher, and a reader who picks it is dropped onto the API docs instead of a wiki.

Run from after_migrate, not patches.txt: `install_app` calls `set_all_patches_as_completed`, so a patch
is stamped executed on a fresh site WITHOUT running — the stray space would survive every prod install.
after_migrate fires on every `bench migrate`, which always follows an install, so this covers a fresh
site and an existing one with the same code.

Only the UNTOUCHED stock space is dropped. If anyone has written a real page under it, it is left alone
and logged: deleting another app's content is not a migrate's business. Our own spaces are addressed by
their own routes (crm-handbook, crm-api-reference) and are never in scope.
"""

import frappe

STOCK_ROUTE = "docs"
STOCK_NAME = "Wiki"
STOCK_PAGE = "Welcome to Frappe Wiki"


def reconcile():
	_drop_stock_wiki_space()


def _drop_stock_wiki_space():
	try:
		space = frappe.db.get_value(
			"Wiki Space",
			{"route": STOCK_ROUTE, "space_name": STOCK_NAME},
			["name", "root_group"],
			as_dict=True,
		)
		if not space:
			return

		pages = frappe.get_all(
			"Wiki Document",
			filters={"parent_wiki_document": space.root_group},
			fields=["name", "title"],
		)
		if any(page.title != STOCK_PAGE for page in pages):
			frappe.log_error(
				title="wiki_reconcile: stock space has real content, left alone",
				message=f"{space.name} ({STOCK_ROUTE}) holds {[p.title for p in pages]}",
			)
			return

		# The tree first, then its root, then the space — a Wiki Document is a nested-set node and a
		# space deleted out from under one leaves it orphaned and unreachable.
		for page in pages:
			frappe.delete_doc("Wiki Document", page.name, force=True, ignore_permissions=True)
		if space.root_group:
			frappe.delete_doc("Wiki Document", space.root_group, force=True, ignore_permissions=True)
		frappe.delete_doc("Wiki Space", space.name, force=True, ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "wiki_reconcile: drop_stock_wiki_space")
