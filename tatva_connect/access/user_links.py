# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A profile link is a secure web address (https://), never a scheme the browser can execute.

The social fields are plain `Data` with no `options`, so frappe's URL validation never runs on
them — and it would not have helped: `validate_url` accepts ANY scheme unless the caller passes
`valid_schemes`, and `base_document._sanitize_content` skips the value because it holds no angle
bracket. The value becomes dangerous only where the profile page binds it to an `href`, so the
check belongs at write time.

Bound as a class override (not a doc_event): this is infrastructure, never a toggleable
automation — same reason as `WhatsApp Account` and `Error Log`.
"""
import frappe
from frappe.core.doctype.user.user import User

from tatva_connect.access.link_scheme import assert_safe_scheme

# Rendered as an anchor href on the profile page — the only User fields reaching the DOM as a link.
LINK_FIELDS = ("linkedin", "github", "twitter", "medium")


class TatvaUser(User):
	def validate(self):
		self.reject_unsafe_profile_links()
		super().validate()

	def reject_unsafe_profile_links(self):
		"""Refuse a profile link that is not https://. Only a CHANGED value is judged,
		so a legacy row saved for an unrelated reason is never blocked."""
		for fieldname in LINK_FIELDS:
			if not self.meta.get_field(fieldname):
				continue
			if not self.has_value_changed(fieldname):
				continue
			assert_safe_scheme(
				self.get(fieldname),
				self.meta.get_label(fieldname),
			)
