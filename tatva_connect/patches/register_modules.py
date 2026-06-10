"""Register modules.txt -> Module Def BEFORE model sync.

`bench migrate` on an EXISTING site does NOT create Module Def records from
modules.txt — add_module_defs() runs only at install-app. A doctype synced into a
not-yet-registered module would then fail its required `module` Link. So register
the modules ourselves, FIRST in patches.txt [pre_model_sync].

Idempotent + safe on fresh install (where install-app already made them) and on a
prod image rebuild.
"""
import frappe
from frappe.installer import add_module_defs


def execute():
	add_module_defs("tatva_connect", ignore_if_duplicate=True)
