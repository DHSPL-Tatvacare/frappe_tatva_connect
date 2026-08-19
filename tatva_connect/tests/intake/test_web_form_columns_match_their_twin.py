# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A field we copy onto the Web Form must be the same KIND of field as the column receiving it.

Rich text may only feed rich text. `success_message` was Text Editor here and plain Text there, where
frappe escapes it (web_form.py:462) — so an author wrote markup and a patient read the tags.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.intake import builder

# The one rename `_SETTINGS_COLUMNS` performs; everything else is copied name-for-name.
_RENAMED = {"introduction": "introduction_text"}

# Frappe stores HTML in these and paints it raw. Anything else is text and is escaped on the way out.
_RICH = {"Text Editor", "HTML Editor", "Markdown Editor"}


class TestWebFormColumnsMatchTheirTwin(FrappeTestCase):
	def test_rich_text_never_feeds_a_plain_column(self):
		ours = frappe.get_meta("CRM Intake Form")
		theirs = frappe.get_meta("Web Form")
		mismatched = []
		for fieldname in builder._SETTINGS_COLUMNS:
			source = ours.get_field(fieldname)
			target = theirs.get_field(_RENAMED.get(fieldname, fieldname))
			if not source or not target:
				continue
			if source.fieldtype in _RICH and target.fieldtype not in _RICH:
				mismatched.append(
					f"CRM Intake Form.{fieldname} is {source.fieldtype}, but Web Form."
					f"{target.fieldname} is {target.fieldtype} — frappe escapes that, so the author's "
					f"markup is printed to the reader as text"
				)
		self.assertEqual(
			mismatched,
			[],
			"A rich-text field is copied onto a plain Web Form column:\n  " + "\n  ".join(mismatched),
		)

	def test_every_copied_column_exists_on_both_sides(self):
		"""A column that vanished from either side is copied into nothing and fails silently."""
		ours = frappe.get_meta("CRM Intake Form")
		theirs = frappe.get_meta("Web Form")
		orphaned = [
			f"{fieldname} -> Web Form.{_RENAMED.get(fieldname, fieldname)}"
			for fieldname in builder._SETTINGS_COLUMNS
			if ours.get_field(fieldname) and not theirs.get_field(_RENAMED.get(fieldname, fieldname))
		]
		self.assertEqual(orphaned, [], "Copied to a Web Form column that does not exist:\n  " + "\n  ".join(orphaned))
