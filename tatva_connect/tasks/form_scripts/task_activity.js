// CRM Form Script (CRM Task, Form view) — complete an assigned activity via its form.
//
// When a rep marks an assigned ACTIVITY task Done from the task modal (its type has a form
// schema), we open the SAME activity form to capture its fields, then save_activity (the one
// server writer) stamps it Done with the details. The quick status dropdown is handled in the
// Lead form script (activity_log.js); this covers the modal-save path.
//
// EVENT-DRIVEN like whatsapp_template.js: onValidate opens the dialog and ABORTS the empty save
// with a plain throw (crm's submit wrapper swallows it silently — no toast). The dialog's own
// Save action does the real write; dismissing it via X/Esc/Cancel simply leaves the task open —
// nothing is awaited, so nothing can hang. The server enforce_activity_logged backstop guarantees
// an activity can never be completed empty even if this controller never ran.
//
// Composes with crm's native crm_task/form.js CRMTask. Native this.createDialog/this.call/
// this.toast + theme tokens only.

const ESC = (s) =>
  String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const DT2LOCAL = (v) => (v && v.length === 16 ? v.replace("T", " ") + ":00" : v);

function getFix() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) return reject(new Error("no geolocation"));
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve({ lat: pos.coords.latitude, lng: pos.coords.longitude, accuracy: pos.coords.accuracy }),
      reject,
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 }
    );
  });
}

function ensureActivityStyle() {
  if (document.getElementById("tc-activity-task-style")) return;
  const st = document.createElement("style");
  st.id = "tc-activity-task-style";
  st.textContent =
    ".tc-af-field{display:flex;flex-direction:column;gap:5px;margin-bottom:12px}" +
    ".tc-af-field>label{font-size:12px;color:var(--ink-gray-6)}.tc-af-field .tc-req{color:var(--ink-red-3)}" +
    ".tc-af-ctrl{width:100%;box-sizing:border-box;border:1px solid var(--outline-gray-2);border-radius:8px;" +
    "padding:7px 10px;font-size:13px;background:var(--surface-white);color:var(--ink-gray-8);outline:none}" +
    ".tc-af-ctrl:focus{border-color:var(--outline-gray-3)}textarea.tc-af-ctrl{min-height:64px;resize:vertical}";
  document.head.appendChild(st);
}

function activityControlHtml(f) {
  const opts = (f.options || "").split("\n").map((o) => o.trim()).filter(Boolean);
  const a = 'class="tc-af-ctrl" data-fieldname="' + ESC(f.fieldname) + '" data-fieldtype="' + ESC(f.fieldtype) + '"';
  if (f.fieldtype === "Small Text") return "<textarea " + a + "></textarea>";
  if (f.fieldtype === "Select")
    return "<select " + a + '><option value=""></option>' +
      opts.map((o) => "<option>" + ESC(o) + "</option>").join("") + "</select>";
  if (f.fieldtype === "Datetime") return '<input type="datetime-local" ' + a + " />";
  if (f.fieldtype === "Check") return '<input type="checkbox" ' + a + ' style="width:auto" />';
  return '<input type="text" ' + a + " />"; // Data, Link
}

function collectValues(schema, toast) {
  const values = {};
  for (const f of schema) {
    const el = document.querySelector('#tc-act-form [data-fieldname="' + CSS.escape(f.fieldname) + '"]');
    let v = !el ? "" : f.fieldtype === "Check" ? (el.checked ? 1 : 0) : el.value;
    if (f.fieldtype === "Datetime") v = DT2LOCAL(v);
    if (f.reqd && (v === "" || v == null)) {
      toast.error(f.label + " is required");
      return null;
    }
    values[f.fieldname] = v;
  }
  return values;
}

// {} (not needed) | {lat,lng,accuracy,address} | null (denied)
async function captureIfNeeded(call, lead, type, toast) {
  let needed = false;
  try {
    needed = await call("tatva_connect.location.api.location_needed", { lead: lead, task_type: type });
  } catch (e) {
    return {};
  }
  if (!needed) return {};
  let fix;
  try {
    fix = await getFix();
  } catch (e) {
    toast.error("Location permission is required for this in-person activity.");
    return null;
  }
  let address = null;
  try {
    const r = await call("tatva_connect.location.api.reverse_geocode", { lat: fix.lat, lng: fix.lng });
    address = r && r.address;
  } catch (e) {}
  return { lat: fix.lat, lng: fix.lng, accuracy: fix.accuracy, address: address };
}

class CRMTask {
  async onValidate() {
    const doc = this.doc;
    if (!doc || doc.reference_doctype !== "CRM Lead" || !doc.custom_task_type) return;
    if ((doc.status || "") !== "Done") return; // only when completing
    if (doc.custom_activity_payload) return; // already logged

    let schema = [];
    try {
      schema = (await this.call("tatva_connect.activity.api.get_schema", { task_type: doc.custom_task_type })) || [];
    } catch (e) {
      return; // can't resolve -> let the native completion + server backstop decide
    }
    if (!schema.length) return; // no activity form -> plain completion

    // Open the form (event-driven) and abort THIS empty save. A plain throw is swallowed by crm's
    // submit wrapper (silent — no toast); the dialog's Save action does the real write.
    this.openActivityDialog(doc, schema);
    throw new Error("__tc_activity_pending__");
  }

  openActivityDialog(doc, schema) {
    ensureActivityStyle();
    const call = this.call;
    const toast = this.toast;
    const lead = doc.reference_docname;
    const type = doc.custom_task_type;
    const fields = schema
      .map(
        (f) =>
          '<div class="tc-af-field"><label>' + ESC(f.label) +
          (f.reqd ? ' <span class="tc-req">*</span>' : "") + "</label>" + activityControlHtml(f) + "</div>"
      )
      .join("");
    this.createDialog({
      title: type,
      html: '<div id="tc-act-form">' + fields + "</div>",
      actions: [
        {
          label: "Save",
          variant: "solid",
          onClick: async (close) => {
            const values = collectValues(schema, toast);
            if (values === null) return;
            const loc = await captureIfNeeded(call, lead, type, toast);
            if (loc === null) return;
            const payload = { ...values, lat: loc.lat, lng: loc.lng, accuracy: loc.accuracy };
            try {
              await call("tatva_connect.activity.api.save_activity", {
                lead: lead, task_type: type, values: JSON.stringify(payload), task: doc.name,
              });
            } catch (e) {
              toast.error((e && e.message) || "Could not save activity"); // incl. out-of-range block
              return;
            }
            close();
            if (loc.lat) this.showLocationFetched(loc); // static-map receipt for in-person captures
            else toast.success("Activity logged · " + type);
          },
        },
        { label: "Cancel", onClick: (close) => close() },
      ],
    });
  }

  // Receipt after a successful save: captured spot on a Google static map (key-safe proxy) +
  // address + accuracy. Event-driven (Done button) — nothing awaited, can't hang.
  showLocationFetched(fix) {
    const mapUrl = "/api/method/tatva_connect.location.api.static_map?lat=" +
      encodeURIComponent(fix.lat) + "&lng=" + encodeURIComponent(fix.lng);
    const acc = fix.accuracy ? "±" + Math.round(fix.accuracy) + " m" : "";
    const addr = fix.address || "Address unavailable";
    this.createDialog({
      title: "Location Captured",
      html:
        '<img src="' + mapUrl + '" alt="map" style="width:100%;height:180px;object-fit:cover;' +
        "border-radius:8px;margin-bottom:12px\" onerror=\"this.style.display='none'\"/>" +
        '<div style="display:flex;gap:8px;align-items:flex-start"><span style="flex:0 0 auto;margin-top:1px">📍</span>' +
        '<div><div style="font-size:14px;line-height:1.5;color:var(--ink-gray-8)">' + ESC(addr) + "</div>" +
        '<div style="color:var(--ink-gray-5);font-size:12px;margin-top:4px">' + ESC(acc) + "</div></div></div>",
      actions: [{ label: "Done", variant: "solid", onClick: (c) => c() }],
    });
  }
}
