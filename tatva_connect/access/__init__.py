"""Access brains — the ONE place each access question is answered.

  * `entitlement.py` — which FIELDS an internal user may see (column-level).
  * `visibility.py`  — which RECORDS an internal user may see (row-level).

Both share `request_cache`, the per-request memoisation primitive defined here so there is
exactly one copy.
"""
import frappe


def request_cache(bucket, key, builder):
	"""Per-request memoisation on `frappe.local` (cleared each request). One read per key."""
	store = getattr(frappe.local, bucket, None)
	if store is None:
		store = {}
		setattr(frappe.local, bucket, store)
	if key not in store:
		store[key] = builder()
	return store[key]
