// Desk Client Script — CRM Task Section (Frappe Desk, /app/crm-task-section).
// Everything shown is decided server-side; this file paints and never judges.
// The four column fields (Row Key, Value, Label, Question) and the Child Table Field all NAME a column, and
// all five were free text. `CRMTaskSection._every_named_column_is_real` already refuses a wrong one — so the
// only thing missing was being told the choices BEFORE typing blind and being refused.
// The candidates come from frappe's OWN meta cache via frappe.model.with_doctype: the section declares which
// doctype's columns its fields are, and that meta is the one place those columns are described. No endpoint
// is added and no list is held here.
// With this script dead the page is fully usable: every painted control is a plain typed field.

// The columns of CRM Task that hold this section's rows — a Table field pointing at the target doctype, which
// is exactly what `_child_table_reaches_the_target` demands on save.
const TATVA_TASK_DOCTYPE = 'CRM Task';

frappe.ui.form.on('CRM Task Section', {
  refresh(frm) {
    tatva_section_column_options(frm);
    tatva_section_table_field_options(frm);
  },
  // The target changed — every column name on this form belongs to the newly chosen doctype.
  target_doctype(frm) {
    tatva_section_column_options(frm);
    tatva_section_table_field_options(frm);
  },
});

// The four fields that name a column OF THE TARGET. One list, so a field added to this doctype is offered by
// having been added to `COLUMN_FIELDS` server-side and here — the same pairing the validator relies on.
const TATVA_SECTION_COLUMN_FIELDS = ['row_key_field', 'value_field', 'label_field', 'question_field'];

function tatva_section_column_options(frm) {
  const target = frm.doc.target_doctype;
  if (!target) {
    TATVA_SECTION_COLUMN_FIELDS.forEach((f) => frm.set_df_property(f, 'options', [{ value: '', label: '' }]));
    return;
  }
  tatva_doctype_fields(target).then((fields) => {
    const opts = [{ value: '', label: '' }, ...fields];
    TATVA_SECTION_COLUMN_FIELDS.forEach((f) => frm.set_df_property(f, 'options', opts));
  });
}

// Child Table Field names a Table field on CRM Task whose options ARE this target — the same test
// `_child_table_reaches_the_target` makes, so only the fields that would pass it are offered.
function tatva_section_table_field_options(frm) {
  const target = frm.doc.target_doctype;
  frappe.model.with_doctype(TATVA_TASK_DOCTYPE, () => {
    const holders = (frappe.get_meta(TATVA_TASK_DOCTYPE).fields || [])
      .filter((f) => f.fieldtype === 'Table' && (!target || f.options === target))
      .map((f) => ({ value: f.fieldname, label: f.label ? f.label + ' (' + f.fieldname + ')' : f.fieldname }));
    frm.set_df_property('child_table_field', 'options', [{ value: '', label: '' }, ...holders]);
  });
}

// {value,label} pairs for every real column of a doctype. `add_options` reads .label/.value, so the admin
// reads the label while the column name is what is stored. Layout rows store nothing and are dropped.
// Cached by frappe's own meta cache, so a second call for the same doctype costs nothing.
const TATVA_LAYOUT_FIELDTYPES = ['Tab Break', 'Section Break', 'Column Break'];

function tatva_doctype_fields(doctype) {
  return new Promise((resolve) => {
    frappe.model.with_doctype(doctype, () => {
      resolve(
        (frappe.get_meta(doctype).fields || [])
          .filter((f) => !TATVA_LAYOUT_FIELDTYPES.includes(f.fieldtype))
          .map((f) => ({ value: f.fieldname, label: f.label ? f.label + ' (' + f.fieldname + ')' : f.fieldname })),
      );
    });
  });
}
