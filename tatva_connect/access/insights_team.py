# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Insights caches a user's team grants in each worker's memory (`@site_cache`, 24h) and a team save clears only the saving worker."""
import frappe


def broadcast_grant_change(doc, method=None):
	# After commit, so no worker re-reads the grants before the change is visible to it.
	frappe.db.after_commit.add(frappe.client_cache.erase_persistent_caches)
