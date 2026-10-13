# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""One Meta CRM dataset: where Facebook leads' stage changes are sent for the Conversion Leads goal.

The token is the dataset's own, generated in Events Manager, and Meta issues it to an app it creates for
the purpose. So nothing here links a CRM Facebook App: that app never issued this token.
"""
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from tatva_connect.lead_sync.conversions import has_sent
from tatva_connect.utils import trim_credentials


def graph_api_versions() -> list[str]:
	"""The Graph versions on offer, read from CRM Facebook App's field so the list lives in one place."""
	options = frappe.get_meta("CRM Facebook App").get_field("graph_api_version").options or ""
	return [v for v in options.split("\n") if v]


class CRMMetaDataset(Document):
	def onload(self):
		self.set_onload("has_sent", has_sent(self.name))

	def validate(self):
		trim_credentials(self, "access_token")
		self.test_event_code = (self.test_event_code or "").strip()
		if self.graph_api_version not in graph_api_versions():
			frappe.throw(_("Graph API Version {0} is not one Facebook Apps offer.").format(self.graph_api_version))
		self._validate_stages()
		# The first enable starts from now; Send Past Days is the only way to start earlier.
		if self.enabled and not self.read_upto:
			self.read_upto = now_datetime()

	def _validate_stages(self):
		"""Each stage once here, only selectable stages, and an enabled stage sends to one dataset only."""
		seen = set()
		for row in self.stages:
			row.meta_event = (row.meta_event or "").strip()
			if row.crm_stage in seen:
				frappe.throw(_("Row {0}: {1} is already listed.").format(row.idx, row.crm_stage))
			seen.add(row.crm_stage)

		if not seen:
			return
		selectable = set(frappe.get_all(
			"CRM Lead Stage", filters={"name": ["in", list(seen)], "selectable": 1}, pluck="name"
		))
		for row in self.stages:
			if row.crm_stage not in selectable:
				frappe.throw(_("Row {0}: {1} groups other stages, so no lead sits at it. Pick one of the stages it groups.").format(row.idx, row.crm_stage))

		if not self.enabled:
			return
		taken = frappe.get_all(
			"CRM Meta Dataset Stage",
			filters={"crm_stage": ["in", list(seen)], "parent": ["!=", self.name or ""], "parenttype": self.doctype},
			fields=["crm_stage", "parent"],
		)
		enabled = set(frappe.get_all(
			self.doctype, filters={"name": ["in", [r.parent for r in taken]], "enabled": 1}, pluck="name"
		)) if taken else set()
		for row in taken:
			if row.parent in enabled:
				frappe.throw(_("{0} already sends to the enabled dataset {1}. A stage sends to one dataset only.").format(row.crm_stage, row.parent))
