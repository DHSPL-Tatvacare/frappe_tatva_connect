"""Lead Sync Source override: discovery + crawl through our Graph layer, failures always logged."""
import frappe

from crm.lead_syncing.doctype.lead_sync_source.facebook import FacebookSyncSource
from crm.lead_syncing.doctype.lead_sync_source.lead_sync_source import LeadSyncSource

from tatva_connect.lead_sync.contract import allowed_field_keys, allowed_programs, contract_of, stage
from tatva_connect.lead_sync.discovery import fetch_and_store_pages
from tatva_connect.lead_sync.graph import api_url, graph_get, redact_tokens, settings


class TatvaFacebookSyncSource(FacebookSyncSource):
	def sync_single_lead(self, lead, raise_exception=False):
		"""Facebook enters the ONE brain by the same door as the partner API: a contract -> ticked field_keys -> _upsert_one."""
		from tatva_connect.api.partner import _split_keys, _upsert_one

		source = frappe.get_cached_doc("Lead Sync Source", self.get_source_name())
		contract = contract_of(source)
		answers = {item["name"]: item["values"][0] for item in lead["field_data"]}

		# A question maps to a catalog field_key ("acq:utm_campaign"); the catalog owns where it lands.
		allowed = allowed_field_keys(contract)
		item = {}
		keys = []
		for question_key, value in answers.items():
			field_key = self.get_form_questions_mapping().get(question_key)
			# Unmapped, empty, or not ticked on the contract: the answer simply has nowhere to land.
			if not field_key or not value or field_key not in allowed:
				continue
			keys.append(field_key)
			stage(item, field_key, value)

		for field_key, value in (
			("lead:facebook_lead_id", lead["id"]),
			("lead:facebook_form_id", self.form_id),
			("lead:custom_source_origin", f"Facebook form: {self.form_id}"),
		):
			keys.append(field_key)
			stage(item, field_key, value)

		parent_fields, child_allow = _split_keys(keys)
		mp = frappe._dict(
			source=contract.source, vertical=contract.vertical,
			crm_group=contract.crm_group, program=contract.program,
		)

		try:
			# is_sysmgr=False + mp set => _collect drops any routing in the payload; grain is forced from mp.
			doc_lead, _action = _upsert_one(
				item, mp, False, parent_fields, child_allow,
				allowed_programs=allowed_programs(contract),
			)
			return doc_lead
		except frappe.UniqueValidationError:
			# facebook_lead_id is globally unique: this exact FB lead is already in.
			self.create_failure_log(lead, "Duplicate")
			if raise_exception:
				raise
		except Exception:
			self.create_failure_log(lead, traceback=redact_tokens(frappe.get_traceback(with_context=True)))
			if raise_exception:
				raise

	def get_api_url(self, endpoint: str) -> str:
		return api_url(endpoint)

	def fetch_leads(self):
		"""Follow Graph's paging cursors; upstream asked for limit=100000 in one shot and silently truncated."""
		params = {
			"access_token": self.access_token,
			"fields": "id,created_time,field_data",
			"limit": settings().lead_page_size or 100,
		}
		if self.last_synced_at:
			timestamp = frappe.utils.data.get_timestamp(self.last_synced_at)
			params["filtering"] = frappe.as_json(
				[{"field": "time_created", "operator": "GREATER_THAN", "value": timestamp}]
			)

		leads = []
		url = self.get_api_url(f"/{self.form_id}/leads")
		seen_urls = set()
		while url and url not in seen_urls:
			seen_urls.add(url)
			response = graph_get(f"lead fetch for form {self.form_id}", url, params)
			leads.extend(response.get("data") or [])
			# `next` is a complete URL carrying its own cursor and token, so the params must not be resent.
			url = ((response.get("paging") or {}).get("next")) or None
			params = {}
		return leads


class TatvaLeadSyncSource(LeadSyncSource):
	def validate(self):
		super().validate()
		if self.enabled and not self.get("api_mapping"):
			frappe.throw(
				frappe._("Select a Contract before enabling — it is what gives every lead from this form its grain."),
				title=frappe._("Contract required"),
			)

	def before_insert(self):
		if self.type == "Facebook" and self.access_token:
			try:
				fetch_and_store_pages(self.access_token)
			except Exception:
				# frappe.throw is a 417 and app.py:412 only snapshots >=500, so this would leave no Error Log row.
				# Roll back first: a plain log_error dies with the transaction the raise is about to discard.
				frappe.db.rollback()
				frappe.log_error(
					title=f"Facebook discovery failed: {self.name or self.type}",
					message=redact_tokens(frappe.get_traceback(with_context=True)),
				)
				frappe.db.commit()  # the log row is now the only pending write
				raise

	def _sync_leads(self):
		"""Log and swallow: upstream's caller drops the traceback, and one bad source must not stop the rest."""
		if not (self.type == "Facebook" and self.access_token):
			return
		if not self.facebook_lead_form:
			frappe.throw(frappe._("Please select a lead gen form before syncing!"))
		try:
			# source_name is what the fold reads its contract off; upstream leaves it None.
			TatvaFacebookSyncSource(
				self.get_password("access_token"), self.facebook_lead_form, source_name=self.name
			).sync()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title=f"Facebook lead sync failed: {self.name}",
				message=redact_tokens(frappe.get_traceback(with_context=True)),
			)
			frappe.db.commit()
			raise
