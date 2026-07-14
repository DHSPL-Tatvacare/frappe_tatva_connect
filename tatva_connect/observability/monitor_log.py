# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Retention for `logs/monitor.json.log`, the one log frappe never rotates.

Frappe's request monitor gives every request and job a uuid (`monitor.get_trace_id`), and that uuid is
what joins a partner's API call to its scan verdict and to its traceback. It is worth having. Its side
effect is not: `monitor.py:132` appends with a plain `open(..., "a")`, not through the rotating logger,
so `RotatingFileHandler` (which caps every other bench log) never touches it. Left alone it grows
without bound inside the container's writable layer.

DB-backed logs need none of this. `CRM API Request Log` and `CRM File Scan Log` are registered with
frappe's own `Log Settings` / `logs_to_clear` and trimmed by its daily cleanup. A file is not a doctype,
so this is the one log that needs a sweep of its own.

SCOPE, stated plainly: `logs/` is NOT a mounted volume, so each container writes its own copy of this
file and holds its own lock. That is internally consistent (a container's sweep and its flush contend
for the same lock over the same file, so no record is lost), and the sweep bounds EACH file rather than
their sum. Mounting a shared `logs/` without also sharing `config/`, where the lock lives, would give
one file guarded by per-container locks: a sweep could then truncate while another container's flush
appends, which is a race that does not exist today. The container's writable layer is also discarded on
every redeploy, which bounds it again.
"""
import gzip
import json
import os
import shutil
import time

import frappe
from frappe.monitor import log_file
from frappe.utils import get_datetime
from frappe.utils.synchronization import filelock

from tatva_connect import automation

SWITCH = "Observability::Monitor::log-sweep"

# Whichever is reached first. The file is telemetry, not an audit record: the durable evidence lives in
# the DB logs, which carry their own 90-day retention.
MAX_BYTES = 1 * 1024 * 1024 * 1024
MAX_AGE_DAYS = 30

_SUFFIX = ".gz"
_PENDING = ".pending"


def _archives(path):
	"""The compressed copies beside the live file."""
	directory = os.path.dirname(path)
	base = os.path.basename(path)
	if not os.path.isdir(directory):
		return []
	return [
		os.path.join(directory, f)
		for f in os.listdir(directory)
		if f.startswith(base + ".") and f.endswith(_SUFFIX)
	]


def _drop_expired(path, now):
	"""Delete archives past the age window. Returns how many went."""
	cutoff = now - (MAX_AGE_DAYS * 86400)
	dropped = 0
	for archive in _archives(path):
		try:
			if os.path.getmtime(archive) < cutoff:
				os.remove(archive)
				dropped += 1
		except OSError:
			continue
	return dropped


def _oldest_record_age(path, now):
	"""Seconds since the oldest record still in the live file, read from line 1.

	The file's own mtime is the LAST write, which on an append-only log is always about now and says
	nothing about how far back the contents reach. Every monitor record carries its own `timestamp` and
	the file is append-ordered, so line 1 is the oldest thing in it. Without this, the 30-day half of the
	policy would never fire on a file that stays under the size cap.
	"""
	try:
		with open(path) as fh:
			first = fh.readline()
		if not first.strip():
			return 0
		stamp = json.loads(first).get("timestamp")
		return max(0, now - get_datetime(stamp).timestamp()) if stamp else 0
	except (OSError, ValueError, TypeError):
		return 0  # unreadable or half-written line: never rotate on a guess


def _detach(path, now):
	"""Copy the live file aside and truncate it in place, holding the lock for as little as possible.

	Truncate, never rename: frappe holds this inode open and appends to it. A rename would leave it
	writing to a file nobody can read and every line after the rotation would be lost, which is the same
	reason logrotate needs `copytruncate` for a file like this.

	Only the raw copy happens under the lock. Frappe's own `monitor.flush()` takes the SAME lock with
	`timeout=5` (monitor.py:131), so compressing a gigabyte while holding it would time out every flush
	that minute. Compression happens afterwards, outside the lock, against a file nothing is writing to.
	"""
	pending = f"{path}.{int(now)}{_PENDING}"
	with filelock("monitor_flush", is_global=True, timeout=30):
		shutil.copyfile(path, pending)
		with open(path, "w"):
			pass  # truncate in place, so the handle frappe holds stays valid
	return pending


def _compress(pending):
	"""Gzip a detached copy and remove the raw one. Nothing is appending to it by now."""
	archive = pending[: -len(_PENDING)] + _SUFFIX
	with open(pending, "rb") as src, gzip.open(archive, "wb") as dst:
		shutil.copyfileobj(src, dst)
	os.remove(pending)
	return archive


def sweep():
	"""Scheduled job: enforce 1 GB or 30 days, whichever is reached first.

	Gated like every scheduled automation (invariant #6). Idempotent: a re-run inside both limits rotates
	nothing and deletes nothing. Never raises into the scheduler, because a log that failed to rotate is
	a log, not an outage.
	"""
	if not automation.is_enabled(SWITCH):
		return

	path = log_file()
	if not os.path.exists(path):
		return

	now = time.time()
	try:
		size = os.path.getsize(path)
		age_days = _oldest_record_age(path, now) / 86400
		# Size alone would let a quiet site keep records for years; age alone would let a busy one reach
		# any size inside a single window. Both, whichever is reached first.
		if size >= MAX_BYTES:
			reason = f"{size / 1024 ** 3:.1f} GB"
		elif age_days >= MAX_AGE_DAYS:
			reason = f"{age_days:.0f} days"
		else:
			reason = None

		archive = _compress(_detach(path, now)) if reason else None
		dropped = _drop_expired(path, now)
	except Exception:
		frappe.log_error(title="Monitor log sweep failed", message=frappe.get_traceback())
		return

	if archive or dropped:
		state = f"rotated at {reason} to {os.path.basename(archive)}" if archive else "within both limits"
		frappe.logger("monitor_log").info(
			f"monitor.json.log: {state}; {dropped} archive(s) past {MAX_AGE_DAYS} days removed"
		)
