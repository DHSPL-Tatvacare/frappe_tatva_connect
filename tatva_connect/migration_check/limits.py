# TEMPORARY — migration reconciliation demo, remove before prod. See REMOVE-ME.md
"""Rate limits on every entry point, in three scopes.

Frappe's own `rate_limit` decorator covers PER IP and is used as-is. It cannot cover per-user —
its `key` argument reads a form field, not the session — so the per-user and site-wide scopes are
built here on `frappe.cache` in exactly the same shape: a fixed window, an INCR, a ceiling.

Three scopes, because each stops a different abuse:

  * PER IP     — one machine hammering the endpoint;
  * PER USER   — one account doing the same from several machines;
  * OVERALL    — everyone together spending the LeadSquared quota the migration itself needs.

Refusals raise `frappe.RateLimitExceededError`, which Frappe answers as 429 with a plain message.
Nothing here queues; a refused call costs nothing and says when to come back.
"""

import frappe
from frappe import _

# Launching a run — the expensive submissions. A batch is up to 400 LeadSquared calls.
RUN_PER_HOUR = 5
RUN_PER_DAY = 15

# A single-lead lookup is 4 LeadSquared calls and is the tool's day-to-day use, so it carries its
# own, higher ceiling. Set these to RUN_PER_HOUR/RUN_PER_DAY to make the two identical.
LOOKUP_PER_HOUR = 30
LOOKUP_PER_DAY = 100

# The whole site together, so no combination of users can drain the LeadSquared quota.
SITE_RUNS_PER_HOUR = 12
SITE_RUNS_PER_DAY = 40

HOUR = 3600
DAY = 86400


def _spend(scope: str, action: str, limit: int, seconds: int, when: str) -> None:
	"""One fixed-window counter. Mirrors Frappe's own rate limiter, keyed by scope instead of IP."""
	key = frappe.cache.make_key(f"mc_rl:{action}:{scope}:{seconds}")
	value = frappe.cache.get(key)
	if not value:
		frappe.cache.setex(key, seconds, 0)
	if frappe.cache.incrby(key, 1) > limit:
		frappe.throw(
			_("Limit reached: at most {0} {1} {2}. Please try again later.").format(
				limit, action.replace("_", " "), when
			),
			frappe.RateLimitExceededError,
		)


def _user() -> str:
	return frappe.session.user or "Guest"


def _ip() -> str:
	return getattr(frappe.local, "request_ip", None) or "no-ip"


def check_lookup() -> None:
	"""A single-lead check. Per user and site-wide; per IP is on the endpoint decorator."""
	_spend(f"user:{_user()}", "lead_checks", LOOKUP_PER_HOUR, HOUR, _("per hour"))
	_spend(f"user:{_user()}", "lead_checks", LOOKUP_PER_DAY, DAY, _("per day"))
	_spend("site", "lead_checks", LOOKUP_PER_HOUR * 6, HOUR, _("per hour across the site"))


def check_run_launch() -> None:
	"""Launching a totals or batch run — the expensive path, limited hardest."""
	_spend(f"user:{_user()}", "checks", RUN_PER_HOUR, HOUR, _("per hour"))
	_spend(f"user:{_user()}", "checks", RUN_PER_DAY, DAY, _("per day"))
	_spend(f"ip:{_ip()}", "checks", RUN_PER_HOUR, HOUR, _("per hour from one address"))
	_spend(f"ip:{_ip()}", "checks", RUN_PER_DAY, DAY, _("per day from one address"))
	_spend("site", "checks", SITE_RUNS_PER_HOUR, HOUR, _("per hour across the site"))
	_spend("site", "checks", SITE_RUNS_PER_DAY, DAY, _("per day across the site"))


def remaining(action: str, limit: int, seconds: int) -> int:
	"""What is left in this window, for showing on the page. Never spends."""
	key = frappe.cache.make_key(f"mc_rl:{action}:user:{_user()}:{seconds}")
	used = frappe.cache.get(key)
	try:
		used = int(used or 0)
	except (TypeError, ValueError):
		used = 0
	return max(0, limit - used)


def snapshot() -> dict:
	"""The caller's own remaining budget, so the page can say so before they click."""
	return {
		"runs_this_hour": remaining("checks", RUN_PER_HOUR, HOUR),
		"runs_today": remaining("checks", RUN_PER_DAY, DAY),
		"lookups_this_hour": remaining("lead_checks", LOOKUP_PER_HOUR, HOUR),
		"lookups_today": remaining("lead_checks", LOOKUP_PER_DAY, DAY),
		"limits": {
			"runs_per_hour": RUN_PER_HOUR,
			"runs_per_day": RUN_PER_DAY,
			"lookups_per_hour": LOOKUP_PER_HOUR,
			"lookups_per_day": LOOKUP_PER_DAY,
		},
	}
