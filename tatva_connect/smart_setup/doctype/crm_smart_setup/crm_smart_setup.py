# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""One Smart Setup: an export that bundles a recipe's records on this site, or an import that checks a bundle and applies it."""
import frappe
from frappe import _
from frappe.core.doctype.file.utils import remove_file_by_url
from frappe.model.document import Document

from tatva_connect.smart_setup import bundle, recipes

_NO_BUNDLE = {"record_count": 0, "source_site": None, "exported_at": None}


class CRMSmartSetup(Document):
	def validate(self):
		self._hold_while_running()
		if self.direction == "Export":
			self.root_doctype = recipes.get(self.recipe)["root"]
			for row in self.roots:
				row.root_doctype = self.root_doctype
			if self.bundle_file and self._records_changed():
				self._drop_built_bundle()
		elif self.has_value_changed("bundle_file"):
			self._reset(**(self._contents(bundle.read(self.file_text("bundle_file"))) if self.bundle_file
			               else {"recipe": None, **_NO_BUNDLE}))

	def onload(self):
		"""What the form may offer now, decided by the server: the next stage, a cut-off run, a restore."""
		from tatva_connect.smart_setup import api

		self.set_onload("next_stage", api.next_stage(self))
		self.set_onload("cut_off", self.status in api.RUNNING and not api.is_running(self))
		self.set_onload("can_restore", api.can_restore(self))

	def file_text(self, field):
		"""A bundle attached in `field`, read through its File row, so storage decides where the bytes live."""
		return frappe.get_doc("File", {"file_url": self.get(field), "attached_to_name": self.name}).get_content()

	def _hold_while_running(self):
		"""A running stage reads this setup as it goes, so nothing on it changes until the stage itself writes its end."""
		from tatva_connect.smart_setup import api

		before = self.get_doc_before_save()
		if before and api.is_running(before) and not self.flags.ends_stage:
			frappe.throw(_("This setup is {0}. Wait for it to end before changing it.").format(_(before.status)),
			             title=_("Run in progress"))

	def _records_changed(self):
		"""The recipe or records differ from the ones the built bundle was made of."""
		before = self.get_doc_before_save()
		return bool(before) and (before.recipe, _records(before)) != (self.recipe, _records(self))

	def _drop_built_bundle(self):
		"""An export edited after Build returns to Draft and loses its file, so nothing stale can be downloaded."""
		remove_file_by_url(self.bundle_file, self.doctype, self.name)
		self._reset(bundle_file=None, **_NO_BUNDLE)

	def _contents(self, found):
		"""What an attached bundle holds, as the form shows it."""
		return {"recipe": found["recipe"], "source_site": found.get("source_site"),
		        "exported_at": found.get("exported_at"), "record_count": bundle.size(found)}

	def _reset(self, **state):
		"""Back to Draft with no verdict, holding `state`: an old result never sits beside a changed input."""
		self.update({"status": "Draft", "error": None, "items": [],
		             **{f"{action}_count": 0 for action in bundle.ACTIONS}, **state})


def _records(doc):
	return [row.record for row in doc.roots or []]
