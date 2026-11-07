frappe.ui.form.on('Tatva Analytics Run Log', {
  setup(frm) {
    // Every time here is the system's own; the zone line under each date would misalign the rows.
    frm.fields.forEach((field) => { if (field.df.fieldtype === 'Datetime') field.df.hide_timezone = 1; });
  },
});
