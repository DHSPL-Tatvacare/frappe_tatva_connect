"""The workflow engine's operator kill switch, dormant by default (constitution A.6).

`ENGINE_SWITCH` gates the whole engine: entry-trigger starts, signal delivery, every wake and the */15
backstop. It ships OFF; the operator turns it on.
"""
import frappe

ENGINE_SWITCH = "Workflow::Engine::run"


def as_system():
	"""A background job acts for the system, not for whoever's save queued it; never inside a request, where it would log the person out."""
	frappe.set_user("Administrator")
