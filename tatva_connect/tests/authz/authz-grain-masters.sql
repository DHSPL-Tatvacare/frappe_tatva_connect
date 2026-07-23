-- authz test grains — missing master rows (idempotent; operator-run per constitution invariant 5)
-- The 5 canonical test grains reference these; recon found them absent on dev. Verticals
-- (Goodflip-Care, Tatvapractice, Goodflip) already exist — only these 2 groups + 1 program are new.
-- INSERT IGNORE keys on the PK (name) so re-running is a safe no-op. Run by hand:
--   docker exec frappedev-frappe-1 bash -lc 'cd /workspace/development/frappe-bench && \
--     bench --site dev.localhost mariadb < /path/to/this.sql'

INSERT IGNORE INTO `tabCRM Group`
  (name, group_name, creation, modified, modified_by, owner, docstatus, idx)
VALUES
  ('India', 'India', NOW(6), NOW(6), 'Administrator', 'Administrator', 0, 0),

INSERT IGNORE INTO `tabCRM Program`
  (name, program_name, creation, modified, modified_by, owner, docstatus, idx, custom_is_drug_program)
VALUES
  ('Inside-Sales', 'Inside-Sales', NOW(6), NOW(6), 'Administrator', 'Administrator', 0, 0, 0);
