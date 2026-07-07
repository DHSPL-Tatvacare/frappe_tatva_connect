"""Widen File.custom_lsq_attachment_id from the default varchar(140) to varchar(500).

This column is the LSQ migration's dedup key = the S3 storage PATH of the attachment
(`/t/solution/content/module/lead/<uuid>/<filename>`). Long lead-file paths exceed 140 chars and made
`File.save` FAIL during the attachments migration (2/781 in a 150-lead sample). 500 covers any real S3
key + filename and stays index-safe under utf8mb4 (500*4=2000B < the 3072B index-key limit). The fixture
carries length=500 for fresh installs; this patch widens EXISTING DBs (UAT/prod) so the full migration
never fails on a long path. Idempotent — only alters when the column is narrower than 500.
"""
import frappe


def execute():
    if not frappe.db.has_column("File", "custom_lsq_attachment_id"):
        return
    cur = frappe.db.sql(
        """SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=%s AND TABLE_NAME='tabFile' AND COLUMN_NAME='custom_lsq_attachment_id'""",
        frappe.conf.db_name,
    )
    if cur and cur[0][0] and int(cur[0][0]) >= 500:
        return                                   # already widened — no-op
    frappe.db.change_column_type("File", "custom_lsq_attachment_id", "varchar(500)")
    frappe.db.commit()
