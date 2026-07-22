# TEMPORARY — migration reconciliation demo, remove before prod. See REMOVE-ME.md
"""The Frappe side, fetched through the real partner API.

Deliberately over HTTP rather than by importing the endpoint functions: it exercises the same path
an external partner uses — auth, scoping, envelope, rate limiter — and it lands in the API logs.
A bug found here is a real partner-facing bug.

GET only. Nothing in this module writes.

Counts come from `data.total` on a `limit=1` list call, so a count costs one small response instead
of paging every row.

THE LIST ENDPOINTS ARE RATE-LIMITED AS BULK, AND `bulk_burst` IS PINNED TO 1 FOR THE WHOLE SITE —
the dedupe unique index is one index across every grain, so the limiter refuses a second concurrent
bulk call rather than queueing it. Issuing the four counts in parallel therefore rate-limits itself.
They run sequentially here, overlapped with the one non-bulk call (`lead_get`).
"""

import time

import requests

TIMEOUT = 30
PAGE_SIZE = 200  # the partner API's own list_page_max
MAX_PAGES = 25  # backstop; 5000 activities on one lead is not a real case
RATE_LIMIT_TRIES = 3
MAX_BACKOFF = 5

# resource -> the partner API method that lists it
LIST_METHODS = {
	"activities": "tatva_connect.api.partner_activity.activity_list",
	"notes": "tatva_connect.api.partner_note.note_list",
	"calls": "tatva_connect.api.partner_call.call_list",
	"files": "tatva_connect.api.partner_file.file_list",
}

LEAD_GET = "tatva_connect.api.partner.lead_get"


class PartnerAPIError(RuntimeError):
	pass


class Partner:
	"""Talks to this site's own partner API over loopback."""

	def __init__(self, base_url: str, token: str, host: str | None = None):
		self.base = base_url.rstrip("/")
		self.session = requests.Session()
		self.session.headers.update({"Authorization": f"token {token}", "Accept": "application/json"})
		if host:
			self.session.headers["Host"] = host

	def close(self) -> None:
		self.session.close()

	def __enter__(self):
		return self

	def __exit__(self, *_exc):
		self.close()
		return False

	def get(self, method: str, params: dict) -> dict:
		"""One GET. Returns the envelope's `data`, or raises with the API's own error message.

		Honours the limiter's own `retry_after` rather than guessing a backoff.
		"""
		url = f"{self.base}/api/method/{method}"

		for attempt in range(RATE_LIMIT_TRIES):
			try:
				resp = self.session.get(url, params=params, timeout=TIMEOUT)
			except requests.Timeout as exc:
				raise PartnerAPIError("Frappe did not answer in time. Try again.") from exc
			except requests.ConnectionError as exc:
				# Usually the site restarting under us. Say that, not the connection pool's internals.
				raise PartnerAPIError(
					"Frappe was unreachable, which usually means the site was restarting. Try again."
				) from exc
			except requests.RequestException as exc:
				raise PartnerAPIError("The Frappe request failed. Try again.") from exc

			try:
				body = resp.json()
			except ValueError:
				raise PartnerAPIError(f"{resp.status_code} from {method}: {resp.text[:200]}") from None

			if body.get("status") == "success":
				return body.get("data") or {}

			err = body.get("error") or {}
			retryable = err.get("code") in ("rate_limited", "server_busy")
			if retryable and attempt < RATE_LIMIT_TRIES - 1:
				time.sleep(min(int(err.get("retry_after") or 1), MAX_BACKOFF))
				continue

			raise PartnerAPIError(err.get("message") or f"{resp.status_code} from {method}")

		raise PartnerAPIError(f"{method} still rate-limited after {RATE_LIMIT_TRIES} attempts")

	def lead(self, name: str) -> dict:
		return self.get(LEAD_GET, {"name": name})

	def count(self, resource: str, lead: str) -> int:
		"""The list envelope carries `total`, so limit=1 is enough to count."""
		data = self.get(LIST_METHODS[resource], {"lead": lead, "limit": 1})
		return int(data.get("total") or 0)

	def activity_types(self, lead: str) -> dict:
		"""Activities grouped by task type: {bare type name -> count}, plus the total.

		One paged read instead of one count call per type — eight separate counts would each queue
		on the bulk gate. `task_type` comes back as a grain-scoped composite key
		("GoodFlip Care::Anaya::::Welcome Call"); the operator only needs the name on the end.
		"""
		by_type: dict[str, int] = {}
		offset, total, guard = 0, 0, 0

		while guard < MAX_PAGES:
			guard += 1
			data = self.get(LIST_METHODS["activities"], {"lead": lead, "limit": PAGE_SIZE, "offset": offset})
			total = int(data.get("total") or 0)
			rows = data.get("activities") or []
			for row in rows:
				name = str(row.get("task_type") or "").split("::")[-1].strip() or "(no type)"
				by_type[name] = by_type.get(name, 0) + 1
			if not data.get("has_more") or not rows:
				break
			offset += len(rows)

		return {"total": total, "by_type": by_type}

	def counts(self, lead: str) -> dict:
		"""All counts, sequentially — see the bulk-burst note at the top of this module.

		The page does not use this: it asks for one resource at a time so it can show progress.
		Kept because it is the obvious way to script a full read.
		"""
		return {res: self.count(res, lead) for res in LIST_METHODS}
