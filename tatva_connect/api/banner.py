# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The operator's platform notice — the same `Website Settings.banner_html` the login page renders."""

import frappe
from frappe.utils import sanitize_html, sha256_hash


def notice():
	"""The current notice as `{html, id}`, or `{}` when there is none. Never raises."""
	try:
		# Request-scoped read: the shared doc cache can hold a stale copy with no expiry after a save.
		html = (frappe.db.get_single_value("Website Settings", "banner_html") or "").strip()
		if not html:
			return {}

		# The operator authors HTML; nh3 is what makes it safe to hand to a browser.
		clean = sanitize_html(html, always_sanitize=True)
		return {"html": clean, "id": sha256_hash(clean)[:12]}
	except Exception:
		# Silent by design: the notice is cosmetic, the boot payload it rides is not.
		return {}
