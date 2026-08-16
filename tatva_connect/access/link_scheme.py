# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The one brain for link-scheme safety. Every value that reaches a browser as an href, src,
redirect, or window.open target MUST be https:// — no http://, no javascript:, no data:,
no file://. Every browser-level bypass (tab/newline stripping, case mixing) is normalised
away before the check.

The name is deliberate: this is about the SCHEME a LINK carries, not about network safety.
`assert_safe_public_url` in utils.py guards outbound HTTP (SSRF — where the server fetches).
This guards inbound rendering (XSS — where the browser navigates). Two different threat
models, two different guards, two different names.

Used by:
  - user_links.py        (profile links: linkedin/github/twitter/medium)
  - link_scheme doc_events (CRM Lead/Deal/Organization website, CRM Intake Form.success_url,
    CRM Maps Settings.osm_tile_url)

Operator-only fields (WhatsApp Account.url, telephony base_url, etc.) are NOT driven
through here — they carry a different contract (internal API endpoints) and are gated by
assert_safe_public_url in utils.py.
"""
from urllib.parse import urlparse

import frappe

# A browser deletes tab/newline/return anywhere in a URL before reading the scheme.
_BROWSER_DROPS = str.maketrans("", "", "\t\n\r")


def normalise(value):
	return (value or "").translate(_BROWSER_DROPS).strip().lower()


def is_safe_scheme(value, *, allow_in_site_path=False):
	"""True when `value` is empty, https://, or — where the field allows it — a path on this site.

	Read with `urlparse`, the same reader frappe's own `validate_url` uses, because the dangerous values
	are told apart by their PARTS and not by a prefix: `//evil.com` carries no scheme yet sends a browser
	off-site, and `javascript:` carries one that frappe's helper accepts. A path on this site is the only
	shape that is scheme-less AND host-less; everything else must name https.

	`allow_in_site_path` is what a NAVIGATION target (a tile, a workspace link) needs — its normal value is
	`/crm/leads`, so the https-only answer would reject every legitimate one. A field that is rendered as an
	outbound address leaves it False and keeps the stricter rule.
	"""
	value = normalise(value)
	if not value:
		return True
	parsed = urlparse(value)
	if not parsed.scheme and not parsed.netloc:
		return allow_in_site_path and value.startswith("/")
	# A host is required with the scheme: `https:evil.com` and `https:/evil.com` name none, and a browser
	# resolves both against the page it is already on rather than the address they appear to carry.
	return parsed.scheme == "https" and bool(parsed.netloc)


def assert_safe_scheme(value, label, *, allow_in_site_path=False):
	"""Raise ValidationError unless `value` passes `is_safe_scheme` for this field's rule."""
	if not value or not value.strip():
		return
	if is_safe_scheme(value, allow_in_site_path=allow_in_site_path):
		return
	message = (
		frappe._("{0} must be a path on this site or a secure https:// address")
		if allow_in_site_path
		else frappe._("{0} must be a secure web address starting with https://")
	)
	frappe.throw(message.format(frappe.bold(label)), title=frappe._("Invalid URL"))


# -- doc_event handler — one entry per link-carrying field -------------------------------------

_LINK_FIELDS = {
	"website": (
		"CRM Lead",
		"CRM Deal",
		"CRM Organization",
	),
	"success_url": (
		"CRM Intake Form",
	),
	# Rendered as the leaflet tile `src` (location/api.py returns it as `tile_url`); operator-set, so https-only at save.
	"osm_tile_url": (
		"CRM Maps Settings",
	),
}


# Navigation targets a person clicks: a tile, a workspace link. An in-site path is the normal value here.
_NAVIGATION_FIELDS = {
	"link": ("Desktop Icon",),
	"logo_url": ("Desktop Icon",),
	"external_link": ("Workspace",),
}


def guard_link_schemes(doc, method=None):
	"""Validate every link field declared for this doc's doctype, each by the rule its field carries.
	Only CHANGED values are judged — a legacy row saved for an unrelated reason is never blocked."""
	for fields, allow_in_site_path in ((_LINK_FIELDS, False), (_NAVIGATION_FIELDS, True)):
		for fieldname, doctypes in fields.items():
			if doc.doctype not in doctypes:
				continue
			if not doc.meta.get_field(fieldname):
				continue
			if not doc.has_value_changed(fieldname):
				continue
			assert_safe_scheme(
				doc.get(fieldname),
				doc.meta.get_label(fieldname),
				allow_in_site_path=allow_in_site_path,
			)
