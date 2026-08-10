# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every URL-bearing field this app OWNS must have a declared home: either the render-scheme brain
(`access/link_scheme._LINK_FIELDS`, https-only at write) or the exempt map below, which names the
outbound/operator fields guarded at their fetch site by `utils.assert_safe_public_url` instead.

The point is drift: `_LINK_FIELDS` is a hand-curated list, so a new `*_url` field added tomorrow would
ship unguarded until someone remembered. This fails the build the moment such a field exists without a
recorded decision — the same lock the rest of `access/` carries. Scoped to tatva_connect's own doctypes
so a crm/frappe version bump can never turn this red for a field we do not control.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access.link_scheme import _LINK_FIELDS

# Outbound / operator / not-rendered URL fields — guarded at the FETCH site (not render), so they are
# deliberately NOT in the render brain. Each entry records WHY, so adding one is a conscious decision.
_FETCH_OR_INTERNAL = {
	"base_url": "outbound provider endpoint — SSRF-guarded at the fetch site (voice/adapters/bolna._safe_base_url, api/telephony)",
	"recording_ref_url": "outbound recording download — assert_safe_public_url before requests.get (call_media)",
	"url": "operator provider base (WhatsApp Account) — outbound, host-allowlisted at fetch (whatsapp/transport)",
	"recording_url": "provider recording URL — never sent raw to the browser; played through a permission-gated proxy",
}


def _is_url_fieldname(name):
	return bool(name) and (name.endswith("_url") or name in ("url", "website"))


class TestRenderUrlSchemeGuarded(FrappeTestCase):
	def test_every_owned_url_field_has_a_declared_home(self):
		guarded = set(_LINK_FIELDS)
		exempt = set(_FETCH_OR_INTERNAL)
		orphans = []
		for dt in frappe.get_all("DocType", filters={"istable": 0}, pluck="name"):
			try:
				meta = frappe.get_meta(dt)
			except Exception:
				continue
			if (frappe.db.get_value("Module Def", meta.module, "app_name") or "") != "tatva_connect":
				continue
			for f in meta.fields:
				if f.fieldtype in ("Data", "Small Text") and _is_url_fieldname(f.fieldname):
					if f.fieldname not in guarded and f.fieldname not in exempt:
						orphans.append(f"{dt}.{f.fieldname}")
		self.assertEqual(
			orphans, [],
			f"URL field(s) with no declared home — add to link_scheme._LINK_FIELDS (rendered, https-only) "
			f"or to _FETCH_OR_INTERNAL in this test (outbound, guarded at fetch): {sorted(orphans)}",
		)
