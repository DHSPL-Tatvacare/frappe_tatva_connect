# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Frappe's write-time XSS filter is SKIPPED for a tag that never closes. This re-runs it without the skip.

`sanitize_html` first asks whether the value "looks like HTML" by handing it to BeautifulSoup's strict
`html.parser` and checking for a tag (`frappe/utils/html_utils.py:162`). An unterminated tag —
`<iframe src="javascript:alert(1)"` with no closing `>` — yields NO tag there, so the value is returned
verbatim and stored raw. Browsers are lenient: at end of input they emit the unclosed tag and run it.
Server says "not HTML", browser says "iframe"; that parser gap is the vulnerability. Measured on frappe
16.23.0, where the desk list view then prints the stored string into HTML unescaped
(`list_view.js:1007-1012`, `frappe.form.formatters.Data` returns it as-is).

NOTHING HERE DECIDES WHAT IS SAFE. `always_sanitize=True` turns off that one shortcut and hands the value
to frappe's OWN allowlist, with frappe's OWN exemptions mirrored below — so this app never grows a second
sanitiser that can disagree with the first.

The trigger is `<` immediately followed by a name character, which is exactly when a browser starts a tag.
`A < B` is left alone because a browser reads it as text too.
"""
import re

import frappe
from frappe.utils.html_utils import is_json, sanitize_html

from tatva_connect import automation

# Ships dormant like every other switch (invariant 6) — so the remediation is INERT until an operator
# enables this row. That is deliberate and it is the operator's call, not the code's.
_SWITCH = "Access::Desk::sanitize"

# `<` then a name character - the HTML spec's own tag-open condition, and the shape frappe's probe misses.
_TAG_OPEN = re.compile(r"<[a-zA-Z!/]")

# Frappe's exemptions, mirrored so a field it spares is spared here (base_document.py:1326-1334).
_EXEMPT_FIELDTYPES = {"Attach", "Attach Image", "Barcode", "Code"}
_EMAIL_CARRIERS = {"Data", "Small Text", "Text"}


def sanitize_unterminated_tags(doc, method=None):
	"""Re-run frappe's sanitiser over this doc and its child rows with the "looks like HTML" shortcut off."""
	if frappe.flags.in_install or not automation.is_enabled(_SWITCH):
		return
	_clean(doc)
	for row in doc.get_all_children():
		_clean(row)


def _clean(doc):
	for fieldname, value in doc.get_valid_dict(ignore_virtual=True).items():
		if not value or not isinstance(value, str) or not _TAG_OPEN.search(value):
			continue
		# Frappe leaves JSON alone; a stored payload is not HTML until something renders it as HTML.
		if is_json(value):
			continue
		df = doc.meta.get_field(fieldname)
		if _is_exempt(doc, df):
			continue
		cleaned = sanitize_html(value, linkify=bool(df) and df.fieldtype == "Text Editor", always_sanitize=True)
		if cleaned != value:
			doc.set(fieldname, cleaned)


def _is_exempt(doc, df):
	if not df:
		return False
	if df.get("ignore_xss_filter"):
		return True
	if df.fieldtype in _EMAIL_CARRIERS and df.get("options") == "Email":
		return True
	if df.fieldtype in _EXEMPT_FIELDTYPES:
		return True
	if doc.docstatus.is_cancelled():
		return True
	return bool(doc.docstatus.is_submitted() and not df.get("allow_on_submit"))
