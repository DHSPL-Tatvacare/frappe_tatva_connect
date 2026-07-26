# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Failed Lead Sync Log override: a retry re-fetches the lead from Meta and folds it the ONE way a crawl does. Every other Facebook doctype resolves through `TatvaFacebookSyncSource`; upstream's `retry_sync` did not, so the one button an operator presses to recover a lead was also the last door into a fold that reads no contract."""
import frappe
from crm.lead_syncing.doctype.failed_lead_sync_log.failed_lead_sync_log import FailedLeadSyncLog
from frappe import _

from tatva_connect.lead_sync.source import TatvaFacebookSyncSource


class TatvaFailedLeadSyncLog(FailedLeadSyncLog):
	@frappe.whitelist()
	def retry_sync(self):
		"""Re-fetch this lead from Meta by its id, then run it through the grain-aware fold. Upstream replays the stored `lead_data` through a bare `FacebookSyncSource` on the user token: the log keeps the lead's id and not the patient so there is no payload to replay, and the bare class reads no contract so a retry that DID work would write a lead with no grain. Meta holds the lead with no retention window on retrieval, so the id is all that was ever needed."""
		if self.type == "Synced":
			frappe.throw(
				_("This lead has already been synced, so there is nothing to retry. Open the CRM Lead it "
				  "created to see it."),
				title=_("Already synced"),
			)
		if not self.source:
			frappe.throw(
				_("This log names no source, so there is nothing to retry it against."),
				title=_("Source required"),
			)
		source = frappe.get_cached_doc("Lead Sync Source", self.source)
		if source.type != "Facebook":
			frappe.throw(_("Only a Facebook source can be retried."), title=_("Not supported"))

		lead_id = self.facebook_lead_id()
		if not lead_id:
			frappe.throw(
				_("This log does not name a Facebook lead, so the lead cannot be fetched again. Re-run the "
				  "source to pick it up on the next crawl."),
				title=_("Lead id missing"),
			)

		# The SAME class, form and credential the scheduled crawl runs on, so a retried lead and a crawled one cannot land with different routing.
		fold = TatvaFacebookSyncSource(
			source.crawl_token(), source.facebook_lead_form, source_name=source.name
		)
		crm_lead = fold.sync_single_lead(fold.fetch_one_lead(lead_id), raise_exception=True)

		self.type = "Synced"
		self.save()
		return crm_lead

	def facebook_lead_id(self):
		"""The lead's id on Meta. `facebook_lead_id` is what the reference carries now; `id` is what rows written before it carry, and those rows are still in the table."""
		data = frappe.parse_json(self.lead_data or "{}") or {}
		return data.get("facebook_lead_id") or data.get("id")
