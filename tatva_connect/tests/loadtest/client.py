# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The partner API as a caller sees it: HTTP, a token, an envelope, and a stopwatch on every call.

The harness talks to the API exactly the way a partner does. It holds no privileged handle, imports
nothing from the app, and knows only what the published contract says: address a record by the `name`
a create hands back, make a retry safe with an Idempotency-Key, and read the outcome out of the
envelope.

Every request is timed and recorded, so the run doubles as the API's first performance measurement.
"""
import hashlib
import time

import requests

from tatva_connect.tests.loadtest.config import BASE_URL, SITE_HOST

METHOD = "/api/method/tatva_connect.api"


def idempotency_key(account, entity, source_id):
	"""Stable per source record, so replaying a run replays responses instead of writing again."""
	raw = f"{account}:{entity}:{source_id}"
	return hashlib.sha1(raw.encode()).hexdigest()


class Call:
	__slots__ = ("action", "code", "endpoint", "message", "ms", "status")

	def __init__(self, endpoint, status, ms, action=None, code=None, message=None):
		self.endpoint = endpoint
		self.status = status
		self.ms = ms
		self.action = action
		self.code = code
		self.message = message

	@property
	def ok(self):
		return self.status == 200 and self.code is None


class Partner:
	def __init__(self, token, timeout=300):
		self.headers = {
			"Host": SITE_HOST,
			"Authorization": token,
			"Content-Type": "application/json",
		}
		self.timeout = timeout
		self.session = requests.Session()
		self.calls = []

	def _record(self, endpoint, resp, ms):
		if resp is None:
			call = Call(endpoint, 0, ms, code="transport", message="request never completed")
			self.calls.append(call)
			return call, {}

		try:
			body = resp.json()
		except ValueError:
			body = {}

		if body.get("status") == "success":
			call = Call(endpoint, resp.status_code, ms, action=body.get("action"))
		else:
			err = body.get("error") or {}
			call = Call(endpoint, resp.status_code, ms,
			            code=err.get("code") or f"http_{resp.status_code}",
			            message=(err.get("message") or resp.text or "")[:200])
		self.calls.append(call)
		return call, body

	def post(self, module, fn, payload, idem=None):
		"""One write. A 429 is retried with the same key, because a throttle is raised before the write."""
		url = f"{BASE_URL}{METHOD}.{module}.{fn}"
		headers = dict(self.headers)
		if idem:
			headers["Idempotency-Key"] = idem
		endpoint = f"{module}.{fn}"

		resp, ms = None, 0.0
		for attempt in range(5):
			started = time.perf_counter()
			try:
				resp = self.session.post(url, headers=headers, json=payload, timeout=self.timeout)
			except requests.RequestException as exc:
				ms = (time.perf_counter() - started) * 1000
				call = Call(endpoint, 0, ms, code="transport", message=f"{type(exc).__name__}: {exc}"[:200])
				self.calls.append(call)
				return call, {}
			ms = (time.perf_counter() - started) * 1000

			if resp.status_code == 429 and attempt < 4:
				retry_after = 2
				try:
					retry_after = int((resp.json().get("error") or {}).get("retry_after") or 2)
				except (ValueError, TypeError):
					pass
				self.calls.append(Call(endpoint, 429, ms, code="rate_limited"))
				time.sleep(min(retry_after, 10))
				continue

			return self._record(endpoint, resp, ms)

		return self._record(endpoint, resp, ms)

	def get(self, module, fn, params):
		"""One read. A 429 is honoured here exactly as it is on a write: the docs tell a partner to
		wait error.retry_after and retry, and a client that only backs off on writes is not the client
		the docs describe. A read that gave up on the first throttle is a bug in the caller."""
		url = f"{BASE_URL}{METHOD}.{module}.{fn}"
		endpoint = f"{module}.{fn}"

		resp, ms = None, 0.0
		for attempt in range(5):
			started = time.perf_counter()
			try:
				resp = self.session.get(url, headers=self.headers, params=params, timeout=self.timeout)
			except requests.RequestException as exc:
				ms = (time.perf_counter() - started) * 1000
				call = Call(endpoint, 0, ms, code="transport", message=f"{type(exc).__name__}: {exc}"[:200])
				self.calls.append(call)
				return call, {}
			ms = (time.perf_counter() - started) * 1000

			if resp.status_code == 429 and attempt < 4:
				retry_after = 2
				try:
					retry_after = int((resp.json().get("error") or {}).get("retry_after") or 2)
				except (ValueError, TypeError):
					pass
				self.calls.append(Call(endpoint, 429, ms, code="rate_limited"))
				time.sleep(min(retry_after, 10))
				continue

			return self._record(endpoint, resp, ms)

		return self._record(endpoint, resp, ms)
