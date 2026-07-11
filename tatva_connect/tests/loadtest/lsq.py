# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A read-only LeadSquared client, scoped to what this harness pulls.

Read-only is enforced by an endpoint allowlist, not by HTTP verb, because LSQ's retrieve APIs are
POST-based. Any endpoint outside config.LSQ_READ_ENDPOINTS raises before a request is made, so no
create, update, capture or delete endpoint is reachable from this module. Nothing is ever written
back to LSQ.

Activities are read through the bulk by-event endpoint rather than the per-lead one. Only the bulk
endpoint returns the `mx_Custom_*` slots the field map is written against; the per-lead endpoint
returns an activity's envelope but not its payload, which would load every activity blank.
"""
import time

import requests

from tatva_connect.tests.loadtest.config import LSQ_BULK_ENDPOINTS, LSQ_READ_ENDPOINTS

ACTIVITY_BY_EVENT = "ProspectActivity.svc/CustomActivity/RetrieveByActivityEvent"


class LSQReadOnlyError(RuntimeError):
	pass


class LSQ:
	def __init__(self, creds, rate_delay=0.4, timeout=90):
		self.base = creds["LSQ_HOST"].rstrip("/") + "/v2"
		self.auth = {"accessKey": creds["LSQ_ACCESS_KEY"], "secretKey": creds["LSQ_SECRET_KEY"]}
		self.rate_delay = float(creds.get("LSQ_RATE_DELAY", rate_delay))
		self.timeout = timeout
		self.session = requests.Session()
		self.calls = 0

	def _call(self, endpoint, params=None, body=None, tries=4):
		if endpoint not in LSQ_READ_ENDPOINTS:
			raise LSQReadOnlyError(f"not an allowlisted read endpoint: {endpoint}")
		method = LSQ_READ_ENDPOINTS[endpoint]
		query = dict(self.auth, **(params or {}))
		url = f"{self.base}/{endpoint}"
		delay = self.rate_delay * (2 if endpoint in LSQ_BULK_ENDPOINTS else 1)

		for attempt in range(tries):
			try:
				if method == "GET":
					resp = self.session.get(url, params=query, timeout=self.timeout)
				else:
					resp = self.session.post(url, params=query, json=(body or {}), timeout=self.timeout)
			except requests.RequestException as exc:
				if attempt == tries - 1:
					raise LSQReadOnlyError(f"network error on {endpoint}: {exc}") from exc
				time.sleep(2 ** attempt)
				continue
			self.calls += 1
			time.sleep(delay)
			if resp.status_code in (429, 502, 503, 504):
				if attempt == tries - 1:
					raise LSQReadOnlyError(f"{resp.status_code} on {endpoint}: {resp.text[:200]}")
				time.sleep(5 * (attempt + 1))
				continue
			if resp.status_code != 200:
				raise LSQReadOnlyError(f"{resp.status_code} on {endpoint}: {resp.text[:300]}")
			return resp.json()

	def activities_by_event(self, event_code, from_date, to_date, page, page_size):
		"""One page of activities on one event code. Rows carry their slots flat, and name the lead
		they belong to in RelatedProspectId."""
		data = self._call(ACTIVITY_BY_EVENT, body={
			"Parameter": {"ActivityEvent": int(event_code), "FromDate": from_date, "ToDate": to_date},
			"Paging": {"PageIndex": page, "PageSize": page_size},
			"Sorting": {"ColumnName": "CreatedOn", "Direction": 1},
		})
		if not isinstance(data, dict):
			return [], 0
		return (data.get("List") or []), int(data.get("RecordCount") or 0)

	def lead(self, prospect_id):
		"""One lead by its ProspectID. LSQ matches the lookup name case-sensitively."""
		rows = self._call("LeadManagement.svc/Leads.Get", body={
			"Parameter": {"LookupName": "ProspectID", "LookupValue": prospect_id, "SqlOperator": "="},
			"Paging": {"PageIndex": 1, "PageSize": 1},
		})
		rows = rows if isinstance(rows, list) else (rows.get("List") or [])
		return rows[0] if rows else None

	def tasks(self, lead_id):
		data = self._call("LeadManagement.svc/RetrieveTaskByLeadId", params={"leadId": lead_id})
		return (data.get("TaskList") or []) if isinstance(data, dict) else []

	def notes(self, lead_id, page_size=200):
		out, page = [], 1
		while True:
			data = self._call("LeadManagement.svc/RetrieveNote", body={
				"Parameter": {"RelatedId": lead_id},
				"Paging": {"PageIndex": page, "PageSize": page_size},
				"Sorting": {"ColumnName": "CreatedOn", "Direction": 1},
			})
			rows = (data.get("List") or data.get("Notes") or []) if isinstance(data, dict) else []
			out += rows
			total = data.get("RecordCount") if isinstance(data, dict) else None
			if len(rows) < page_size or (total is not None and len(out) >= total):
				return out
			page += 1
