# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Delete the Number Cards and Dashboard Charts the desk homes rework left out of the fixtures, which a fixture never deletes; one a Workspace or Dashboard places is kept."""
import frappe

# Renamed or retired by the desk homes rework (each desk opens on totals, activity and trend cards).
_CARDS = (
	"API Error Rate (24h)", "API p95 Latency (24h)", "Automation Faults 7d", "Calls With No Lead 7d", "Calls With No Media Row 7d",
	"Errors Today", "Inbound Dropped 7d", "Integrations Silent (24h)", "Jobs Failed (Retained)", "Lanes Without a Worker",
	"Leads In 30d", "Leads In 7d", "Leads In Today", "Receipts With No Message 30d", "Recordings Not Yet Published",
	"Recordings To Re-fetch", "WhatsApp Media To Re-fetch", "WhatsApp Sends With No Row 30d",
)
_CHARTS = (
	"API Errors per Day", "API Requests per Day", "API p95 Latency (Daily)", "Automation Health by Grain", "Call Outcomes",
	"External Leads by Source", "FB Sync Failures per Day", "Failed Journeys by Flow", "Failed Journeys per Day",
	"File Scans by Verdict", "Journeys per Day", "Queue Lanes", "Runs by Flow", "Scheduled Jobs", "WhatsApp Faults by Type",
)


def execute():
	cards = set(frappe.get_all("Workspace Number Card", pluck="number_card_name")) | set(frappe.get_all("Number Card Link", pluck="card"))
	charts = set(frappe.get_all("Workspace Chart", pluck="chart_name")) | set(frappe.get_all("Dashboard Chart Link", pluck="chart"))
	for doctype, names, placed in (("Number Card", _CARDS, cards), ("Dashboard Chart", _CHARTS, charts)):
		for name in names:
			if name in placed or not frappe.db.exists(doctype, name):
				continue
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_missing=True)  # authz-ok: tier-a — patch, runs at migrate
