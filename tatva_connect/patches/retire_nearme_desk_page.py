"""Retire the Desk "near-me" Page (NM-06). It was a second Near Me implementation whose `leads_near` endpoint carried NO gate — no `Location::NearMe::directory` switch, no `Field Map User` role — so it bypassed the dormant-by-default boundary the SPA page enforces. The SPA (near_me/api.py) is the one brain; the page module and endpoint are deleted from source, and this clears the imported Page row so a stale Desk shortcut 404s instead of half-rendering. End state declared, nothing assumed: ignore_missing keeps it idempotent on sites that never imported the page."""
import frappe


def execute():
	frappe.delete_doc("Page", "near-me", ignore_missing=True, force=True, ignore_permissions=True)  # authz-ok: tier-a — migration cleanup of a retired desk artefact
