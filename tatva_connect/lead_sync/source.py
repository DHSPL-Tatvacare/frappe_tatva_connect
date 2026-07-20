"""Lead Sync Source override: discovery + crawl through our Graph layer, failures always logged."""
from zoneinfo import ZoneInfo

import frappe
from crm.lead_syncing.doctype.lead_sync_source.facebook import FacebookSyncSource
from crm.lead_syncing.doctype.lead_sync_source.lead_sync_source import LeadSyncSource
from frappe.utils import (
	convert_utc_to_system_timezone,
	cstr,
	get_datetime,
	get_system_timezone,
	now_datetime,
)

from tatva_connect.lead_sync.contract import (
	allowed_field_keys,
	allowed_programs,
	contract_of,
	screening_key,
	stage,
)
from tatva_connect.lead_sync.discovery import fetch_and_store_pages
from tatva_connect.lead_sync.drift import report_form_drift
from tatva_connect.lead_sync.graph import api_url, graph_get, redact_tokens, settings
from tatva_connect.lead_sync.token import page_of_form, refresh_credential

# A checkbox question answers with several values and every one of them is the record; the joined string
# is what a person reading the answer would write down.
ANSWER_JOIN = ", "

# The drift check lists every form on the Page; marketing publishes one every few weeks, not every crawl.
DRIFT_CHECK_CACHE = "tatva_connect:drift_checked"
DRIFT_CHECK_EVERY_SEC = 24 * 60 * 60


def answers(lead):
	"""(raw question, answer) for every question the person ANSWERED, in the order Graph sent them.

	A question the person skipped comes back with NO `values` key at all, so it is absent here and writes
	nothing; a question answered blank comes back with an empty list and is kept, because absent and blank
	are different facts about a patient and only one of them is a gap in what we asked."""
	return [
		(a["name"], ANSWER_JOIN.join(cstr(v) for v in a["values"]))
		for a in lead.get("field_data") or []
		if "values" in a
	]


class TatvaFacebookSyncSource(FacebookSyncSource):
	def sync_single_lead(self, lead, raise_exception=False):
		"""Facebook enters the ONE brain by the same door as the partner API: a contract -> ticked field_keys -> _upsert_one.

		The WHOLE body is guarded, not just the upsert. `contract_of` throws when the contract is missing or
		disabled, `answers()` indexes Graph's payload, and both used to run outside the try — so one malformed
		lead escaped the loop, rolled the pass back, and took every lead already landed with it."""
		from tatva_connect.api.partner import _split_keys, _upsert_one

		try:
			source = frappe.get_cached_doc("Lead Sync Source", self.get_source_name())
			contract = contract_of(source)

			# A contact question maps to a catalog field_key ("lead:mobile_no") and the catalog owns where it
			# lands. Everything else is a screening answer, kept as it was asked and mapped to nothing.
			allowed = allowed_field_keys(contract)
			mapping = self.get_form_questions_mapping()
			screening = screening_key()
			labels = self.question_labels()
			item = {}
			keys = []
			refused = []
			for question, value in answers(lead):
				field_key = mapping.get(question)
				if field_key:
					# A mapped question is a contact field the contract decides; not ticked means dropped, never re-routed.
					if field_key in allowed:
						keys.append(field_key)
						stage(item, field_key, value)
					else:
						refused.append(field_key)
					continue
				# The question is the identity, so nothing has to be declared before an answer can be kept.
				if screening:
					keys.append(screening)
					stage(item, screening, value, question=question,
					      label=labels.get(question) or question, form=self.form_id)
			if refused:
				self.log_refused_fields(refused)

			# Keyed by Meta's submission time, so a re-crawl updates the same touch instead of minting another.
			touched_at = self.site_time(lead.get("created_time")) or now_datetime()
			for field_key, value in (
				("lead:facebook_lead_id", lead["id"]),
				("lead:facebook_form_id", self.form_id),
				("lead:custom_source_origin", f"Facebook form: {self.form_id}"),
				("acq:touch_at", touched_at),
				("acq:utm_source", "facebook"),
				("acq:utm_campaign", self.form_name() or self.form_id),
			):
				keys.append(field_key)
				stage(item, field_key, value)

			parent_fields, child_allow = _split_keys(keys)
			mp = frappe._dict(
				source=contract.source, vertical=contract.vertical,
				crm_group=contract.crm_group, program=contract.program,
			)
			# is_sysmgr=False + mp set => _collect drops any routing in the payload; grain is forced from mp.
			doc_lead, _action = _upsert_one(
				item, mp, False, parent_fields, child_allow,
				allowed_programs=allowed_programs(contract),
			)
			return doc_lead
		except frappe.UniqueValidationError:
			# facebook_lead_id is globally unique: this exact FB lead is already in.
			self.log_failure(lead, "Duplicate")
			if raise_exception:
				raise
		except Exception:
			self.log_failure(lead, traceback=redact_tokens(frappe.get_traceback(with_context=True)))
			if raise_exception:
				raise

	def log_refused_fields(self, refused):
		"""A question mapped to a field the contract does not tick. Said out loud once per crawl, because it
		is an operator config error and the answer is being dropped on the floor until it is fixed."""
		if getattr(self, "_refused_logged", None):
			return
		self._refused_logged = True
		frappe.log_error(
			title="lead_sync: mapped field is not granted by the contract",
			message=f"source={self.get_source_name()} form={self.form_id} dropped={sorted(set(refused))}",
		)

	def log_failure(self, lead, type="Failure", traceback=None):
		"""Roll back, log, then commit — the pattern `observability/capture.py` proved. A failure log written
		inside the doomed transaction dies with it, which is why a wedged form used to leave no trace of WHICH
		lead broke it. After the rollback the log row is the only pending write, so the commit carries it alone."""
		frappe.db.rollback()
		self.create_failure_log(self.failure_reference(lead), type, traceback)
		frappe.db.commit()

	@staticmethod
	def failure_reference(lead):
		"""What the log needs to identify the lead, and nothing else.

		Upstream stores the ENTIRE Graph payload — the patient's name, their phone, every screening answer —
		as JSON in a log table carrying its doctype's default permissions. The id is what an operator needs
		to re-fetch the lead from Meta and see what happened; the body of it is not theirs to keep here."""
		return {
			"facebook_lead_id": (lead or {}).get("id"),
			"created_time": (lead or {}).get("created_time"),
			"answered_questions": len((lead or {}).get("field_data") or []),
		}

	def sync(self):
		"""One lead, one transaction; the watermark moves only over leads this pass actually handled.

		Upstream commits nothing per lead and then stamps the watermark with `now()` — so a crawl that died
		half way still claimed every lead up to the clock, and the ones it never reached were skipped for good."""
		watermark = None
		for lead in self.fetch_leads():
			self.sync_single_lead(lead)
			frappe.db.commit()
			created = lead.get("created_time")
			if created and (watermark is None or get_datetime(created) > get_datetime(watermark)):
				watermark = created
		if watermark:
			self.update_last_synced_at(watermark)

	@staticmethod
	def site_time(graph_time):
		"""A Graph timestamp on the site clock, ready for a Datetime column. None in, None out.

		Graph sends ISO with an offset; frappe's own `get_datetime` parses it and
		`convert_utc_to_system_timezone` moves it onto the site clock. The zone is dropped last because a
		Datetime column stores site-naive. One converter, because two callers need it — the watermark and
		the acquisition touch — and two copies of a timezone rule is how a clock silently drifts."""
		if not graph_time:
			return None
		return convert_utc_to_system_timezone(get_datetime(graph_time)).replace(tzinfo=None)

	def update_last_synced_at(self, upto=None):
		"""Stamp the watermark with the newest lead handled, not the wall clock."""
		if not upto:
			return super().update_last_synced_at()
		frappe.db.set_value(
			"Lead Sync Source",
			self.source_name or {"facebook_lead_form": self.form_id},
			"last_synced_at",
			self.site_time(upto),
		)

	def form_name(self):
		"""The campaign name marketing gave the form, read once per crawl like the question labels.

		Recorded as `utm_campaign` so the acquisition row reads as a campaign and not as an id. Falls back
		to the form id at the call site, because a form with no name still has to be attributable."""
		if getattr(self, "_form_name", None) is None:
			self._form_name = frappe.db.get_value("Facebook Lead Form", self.form_id, "form_name") or ""
		return self._form_name

	def synced_upto_unix(self):
		"""The watermark as Unix seconds, for Graph's `time_created` filter.

		NOT frappe's `get_timestamp`: that is `mktime(getdate(x).timetuple())`, which drops the time of day —
		so the filter meant midnight of the last-synced DAY and every pass refetched and re-saved the whole
		day. `last_synced_at` is naive on the site clock, so the zone is attached before the epoch conversion,
		the same way frappe attaches one in `convert_utc_to_timezone`."""
		stamp = get_datetime(self.last_synced_at).replace(tzinfo=ZoneInfo(get_system_timezone()))
		return stamp.timestamp()

	def question_labels(self) -> dict:
		"""The wording the patient saw, per question key, read once per crawl and cached like the mapping.
		Stored on the answer so a row stays readable after its form is gone."""
		if getattr(self, "_question_labels", None) is None:
			self._question_labels = {
				q["key"]: q["label"]
				for q in frappe.db.get_all(
					"Facebook Lead Form Question",
					filters={"parent": self.form_id},
					fields=["key", "label"],
				)
				if q["label"]
			}
		return self._question_labels

	def get_api_url(self, endpoint: str) -> str:
		return api_url(endpoint)

	def fetch_leads(self):
		"""Follow Graph's paging cursors; upstream asked for limit=100000 in one shot and silently truncated."""
		params = {
			"fields": "id,created_time,field_data",
			"limit": settings().lead_page_size or 100,
		}
		if self.last_synced_at:
			params["filtering"] = frappe.as_json(
				[{"field": "time_created", "operator": "GREATER_THAN", "value": self.synced_upto_unix()}]
			)

		leads = []
		url = self.get_api_url(f"/{self.form_id}/leads")
		seen_urls = set()
		while url and url not in seen_urls:
			seen_urls.add(url)
			response = graph_get(f"lead fetch for form {self.form_id}", url, params, self.access_token)
			leads.extend(response.get("data") or [])
			# `next` is a complete URL carrying its own cursor, so the params must not be resent.
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
		if self.enabled and self.type == "Facebook":
			self._identity_question_must_be_mapped()
		# Graph is asked only when the token is new or changed, never on every save.
		if self.type == "Facebook" and (self.is_new() or self.has_value_changed("access_token")):
			refresh_credential(self)

	def _identity_question_must_be_mapped(self):
		"""A form whose phone question is unmapped cannot produce one lead: `_upsert_one` addresses a lead by
		the dedup anchor and refuses without it, so every lead would be logged and dropped.

		`CRMFacebookLeadForm` holds this same rule, but stands itself down for a discovery write — which is
		exactly how a duplicated form arrives, with a new id and every question unmapped. The source is the
		second door, checked when the operator enables it, and it reads the SAME `IDENTITY_KEY`."""
		from tatva_connect.lead_sync.form import IDENTITY_KEY

		if not self.facebook_lead_form:
			return
		mapped = frappe.get_all(
			"Facebook Lead Form Question",
			filters={"parent": self.facebook_lead_form},
			pluck="mapped_to_crm_field",
		)
		# Nothing discovered yet is not an unmapped form — there is simply nothing to judge.
		if not mapped:
			return
		if IDENTITY_KEY not in mapped:
			frappe.throw(
				frappe._("No question on this form is mapped to {0}, so every lead from it would be refused. "
				         "Map the phone question before enabling this source.").format(IDENTITY_KEY),
				title=frappe._("Phone question not mapped"),
			)

	def crawl_token(self) -> str:
		"""The credential the crawl runs on: the Page token, which does not expire once derived from a
		long-lived user token. The user token is a bootstrap credential and is the fallback only while a
		Page has not been discovered yet."""
		_page, page_token = page_of_form(self.facebook_lead_form)
		return page_token or self.get_password("access_token")

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

	def drift_check_due(self):
		"""Once a day per source, not once a crawl.

		The check asks Facebook for the Page's entire form list with every question expanded — the heaviest
		call the crawl makes — and its answer changes when marketing publishes a form, not every five
		minutes. At a 5-minute frequency that was 288 of them a day per source, against a quota Meta prices
		by cost and charges for failures too.

		Throttled at the CALLER, so the check itself is unchanged and still says exactly what it said. The
		clock is frappe's own cache with a TTL rather than a column on the source: losing the key to an
		eviction costs one extra listing, which is the cheapest possible way to be wrong."""
		key = f"{DRIFT_CHECK_CACHE}:{self.name}"
		if frappe.cache().get_value(key):
			return False
		frappe.cache().set_value(key, 1, expires_in_sec=DRIFT_CHECK_EVERY_SEC)
		return True

	def _sync_leads(self):
		"""Log and swallow: upstream's caller drops the traceback, and one bad source must not stop the rest."""
		if not (self.type == "Facebook" and self.access_token):
			return
		if not self.facebook_lead_form:
			frappe.throw(frappe._("Please select a lead gen form before syncing!"))
		if self.drift_check_due():
			report_form_drift(self)
		try:
			# source_name is what the fold reads its contract off; upstream leaves it None.
			TatvaFacebookSyncSource(
				self.crawl_token(), self.facebook_lead_form, source_name=self.name
			).sync()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title=f"Facebook lead sync failed: {self.name}",
				message=redact_tokens(frappe.get_traceback(with_context=True)),
			)
			frappe.db.commit()
			raise
