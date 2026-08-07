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
  - link_scheme doc_events (CRM Lead/Deal/Organization website, CRM Intake Form.success_url)

Operator-only fields (WhatsApp Account.url, telephony base_url, etc.) are NOT driven
through here — they carry a different contract (internal API endpoints) and are gated by
assert_safe_public_url in utils.py.
"""
import frappe

ALLOWED_PREFIXES = ("https://",)

# A browser deletes tab/newline/return anywhere in a URL before reading the scheme.
_BROWSER_DROPS = str.maketrans("", "", "\t\n\r")


def normalise(value):
	return (value or "").translate(_BROWSER_DROPS).strip().lower()


def is_safe_scheme(value):
	"""True when `value` is empty or starts with ALLOWED_PREFIXES — emptied first,
	normalised as the browser sees it."""
	return not normalise(value) or normalise(value).startswith(ALLOWED_PREFIXES)


def assert_safe_scheme(value, label):
	"""Raise ValidationError unless `value` is empty or https://"""
	if not value or not value.strip():
		return
	if not is_safe_scheme(value):
		frappe.throw(
			frappe._("{0} must be a secure web address starting with https://").format(
				frappe.bold(label)
			),
			title=frappe._("Invalid URL"),
		)


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
}


def guard_link_schemes(doc, method=None):
	"""Validate every link field declared in _LINK_FIELDS for this doc's doctype.
	Only CHANGED values are judged — a legacy row saved for an unrelated reason is never blocked."""
	fieldnames = [f for f, doctypes in _LINK_FIELDS.items() if doc.doctype in doctypes]
	for fieldname in fieldnames:
		df = doc.meta.get_field(fieldname)
		if not df:
			continue
		if not doc.has_value_changed(fieldname):
			continue
		value = doc.get(fieldname)
		assert_safe_scheme(value, doc.meta.get_label(fieldname))
