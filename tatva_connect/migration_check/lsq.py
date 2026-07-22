# TEMPORARY — migration reconciliation demo, remove before prod. See REMOVE-ME.md
"""Read-only LeadSquared client, scoped to one lead.

READ-ONLY IS ENFORCED BY AN ENDPOINT ALLOWLIST, NOT BY HTTP VERB. LSQ's retrieve APIs are POST, so
verb-checking would prove nothing. Anything not in `READ_ONLY_ENDPOINTS` raises before a request is
built. No create/update/capture/delete endpoint is reachable from here.

Auth is accessKey + secretKey as query params — that is LSQ's own scheme for the v2 Sales APIs.
"""

import time

import requests

TIMEOUT = 30
RETRY_STATUSES = (429, 502, 503, 504)
MAX_TRIES = 3

# Seconds to wait after every call. One interactive lookup is 4 calls and needs no pacing; a
# 100-lead batch is 400, and LeadSquared will throttle that. Batch callers pass a delay.
DEFAULT_DELAY = 0.0
BATCH_DELAY = 0.5
MAX_RETRY_AFTER = 30

# path (relative to /v2/) -> the HTTP method this READ endpoint uses
READ_ONLY_ENDPOINTS = {
	"LeadManagement.svc/Leads.Get": "POST",
	"LeadManagement.svc/RetrieveNote": "POST",
	"LeadManagement.svc/RetrieveTaskByLeadId": "GET",
	"ProspectActivity.svc/Retrieve": "POST",
	# Account-wide activity counts for the control totals. Returns RecordCount, so one call
	# prices an entire event code however large it is.
	"ProspectActivity.svc/CustomActivity/RetrieveByActivityEvent": "POST",
}

# LeadSquared throttles these as its "Bulk API" class, on a separate and lower limit — pace 2x.
BULK_ENDPOINTS = frozenset({"ProspectActivity.svc/CustomActivity/RetrieveByActivityEvent"})


class LSQError(RuntimeError):
	pass


class ReadOnlyViolation(LSQError):
	"""A non-allowlisted endpoint was requested. Never reaches the network."""


class Client:
	"""One short-lived client per lookup. Not shared across requests or threads."""

	def __init__(self, host: str, access_key: str, secret_key: str, delay: float = DEFAULT_DELAY):
		self.base = f"{host.rstrip('/')}/v2"
		self._auth = {"accessKey": access_key, "secretKey": secret_key}
		self.delay = delay
		self.calls = 0  # so a job can report exactly what it spent
		self.session = requests.Session()

	def close(self) -> None:
		self.session.close()

	def __enter__(self):
		return self

	def __exit__(self, *_exc):
		self.close()
		return False

	def _call(self, endpoint: str, params: dict | None = None, body: dict | None = None):
		method = READ_ONLY_ENDPOINTS.get(endpoint)
		if method is None:
			raise ReadOnlyViolation(f"Refusing non-allowlisted endpoint: {endpoint}")

		query = dict(self._auth)
		if params:
			query.update(params)
		url = f"{self.base}/{endpoint}"

		last = None
		for attempt in range(MAX_TRIES):
			try:
				self.calls += 1
				if method == "GET":
					resp = self.session.get(url, params=query, timeout=TIMEOUT)
				else:
					resp = self.session.post(url, params=query, json=(body or {}), timeout=TIMEOUT)
			except requests.RequestException as exc:
				last = LSQError("LeadSquared could not be reached. Try again.")
				last.__cause__ = exc
				continue
			finally:
				if self.delay:
					time.sleep(self.delay * (2 if endpoint in BULK_ENDPOINTS else 1))

			if resp.status_code in RETRY_STATUSES and attempt < MAX_TRIES - 1:
				last = LSQError(f"{resp.status_code} on {endpoint}")
				# Obey LeadSquared's own back-off when it sends one; guess only when it does not.
				time.sleep(_retry_after(resp, attempt))
				continue
			if resp.status_code != 200:
				raise LSQError(f"{resp.status_code} on {endpoint}: {resp.text[:200]}")
			return resp.json()

		raise last or LSQError(f"exhausted retries on {endpoint}")

	# -- the four reads ------------------------------------------------------

	def lead(self, prospect_id: str) -> dict | None:
		"""The lead record. LookupName is case-sensitive."""
		rows = self._call(
			"LeadManagement.svc/Leads.Get",
			body={
				"Parameter": {"LookupName": "ProspectID", "LookupValue": prospect_id, "SqlOperator": "="},
				"Paging": {"PageIndex": 1, "PageSize": 1},
			},
		)
		return rows[0] if rows else None

	def notes(self, prospect_id: str) -> list[dict]:
		body = {
			"Parameter": {"RelatedId": prospect_id},
			"Paging": {"PageIndex": 1, "PageSize": 200},
			"Sorting": {"ColumnName": "CreatedOn", "Direction": 1},
		}
		payload = self._call("LeadManagement.svc/RetrieveNote", body=body)
		return payload.get("List") or []

	def tasks(self, prospect_id: str) -> list[dict]:
		payload = self._call("LeadManagement.svc/RetrieveTaskByLeadId", params={"leadId": prospect_id})
		return payload.get("TaskList") or []

	def activities(self, prospect_id: str) -> list[dict]:
		"""Every activity for the lead, with its event code and any file URLs.

		PageSize MUST be set. This endpoint defaults to 25 and silently drops the rest.
		"""
		body = {
			"Paging": {"PageIndex": 1, "PageSize": 1000},
			"Sorting": {"ColumnName": "CreatedOn", "Direction": 1},
		}
		payload = self._call(
			"ProspectActivity.svc/Retrieve",
			params={"leadId": prospect_id, "getFileURL": "true"},
			body=body,
		)
		return payload.get("ProspectActivities") or []


def _retry_after(resp, attempt: int) -> float:
	"""LeadSquared's Retry-After if present and sane, else a widening back-off."""
	raw = resp.headers.get("Retry-After")
	if raw:
		try:
			return min(float(raw), MAX_RETRY_AFTER)
		except (TypeError, ValueError):
			pass
	return min(2 ** (attempt + 1), MAX_RETRY_AFTER)


def event_code(activity: dict) -> str:
	"""This endpoint calls it EventCode; the bulk one calls it ActivityEvent. Accept either."""
	raw = activity.get("EventCode")
	if raw in (None, ""):
		raw = activity.get("ActivityEvent")
	return "" if raw in (None, "") else str(raw)


def activity_fields(activity: dict) -> dict:
	"""Per-lead activities nest their custom values under ActivityFields."""
	fields = activity.get("ActivityFields")
	return fields if isinstance(fields, dict) else {}
