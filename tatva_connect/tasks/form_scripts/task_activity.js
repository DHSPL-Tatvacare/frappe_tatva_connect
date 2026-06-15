// CRM Form Script (CRM Task, Form view) — complete an assigned activity via its form.
//
// When a rep marks an assigned ACTIVITY task Done (its type is an activity type that has
// a form schema), the SAME dynamic form the "Log Activity" entry uses opens to capture the
// activity's fields, then activity.api.save_activity (the ONE server writer) splits them
// into first-class columns + JSON payload and stamps the task Done. We then abort the
// native save with a plain throw — crm's submit wrapper swallows it silently (see
// data/document.js), so save_activity's write stands and there is no double write.
//
// In-person activities on a location-tracked grain also capture GPS here (Phase B): the same
// fix flows through save_activity, which runs the clinic-anchor + radius guard (location.api).
// Composes with crm's native crm_task/form.js CRMTask (crm runs every CRMTask controller).
// Native this.createDialog / this.call / this.toast / this.throwError + theme tokens only. Plain
// tasks and activity types without a schema are untouched (normal completion).

const ESC = (s) =>
  String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const DT2LOCAL = (v) => (v && v.length === 16 ? v.replace("T", " ") + ":00" : v);

// Device GPS read (browser-only API). Server owns the rule + radius guard (location.api).
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

class CRMTask {
  async onValidate() {
    await this.captureActivityOnComplete();
  }

  async captureActivityOnComplete() {
    const doc = this.doc;
    if (!doc || doc.reference_doctype !== "CRM Lead" || !doc.custom_task_type) return;
    if ((doc.status || "") !== "Done") return; // only when completing
    if (doc.custom_activity_payload) return; // already captured

    let schema = [];
    try {
      schema = (await this.call("tatva_connect.activity.api.get_schema", { task_type: doc.custom_task_type })) || [];
    } catch (e) {
      return; // not resolvable -> let the native completion proceed
    }
    if (!schema.length) return; // no activity form -> plain completion

    const values = await this.collectActivity(doc.custom_task_type, schema);
    if (values === null) {
      this.throwError("Activity details are required to complete this task.");
    }

    const loc = await this.captureIfNeeded(doc.custom_task_type, doc.reference_docname);
    if (loc === null) {
      this.throwError("Location capture is required to complete this in-person activity.");
    }

    try {
      await this.call("tatva_connect.activity.api.save_activity", {
        lead: doc.reference_docname, task_type: doc.custom_task_type,
        values: JSON.stringify({ ...values, ...loc }), task: doc.name,
      });
    } catch (e) {
      this.throwError((e && e.message) || "Could not save activity."); // incl. out-of-range block
    }
    this.toast.success("Activity logged · " + doc.custom_task_type);
    // save_activity already persisted the split + Done; abort the native save (silent —
    // crm's submit wrapper swallows a plain throw) so we never double-write.
    throw new Error("__tc_activity_saved__");
  }

  // Capture+confirm a fix for an in-person tracked type; returns {lat,lng,accuracy} | {} (not
  // needed) | null (denied/cancelled -> caller blocks completion).
  async captureIfNeeded(type, lead) {
    let needed = false;
    try {
      needed = await this.call("tatva_connect.location.api.location_needed", { lead, task_type: type });
    } catch (e) {
      return {}; // probe failed -> server backstop enforces; send no fix
    }
    if (!needed) return {};
    let fix;
    try {
      fix = await getFix();
    } catch (e) {
      return null;
    }
    let address = null;
    try {
      const r = await this.call("tatva_connect.location.api.reverse_geocode", { lat: fix.lat, lng: fix.lng });
      address = r && r.address;
    } catch (e) {}
    const okay = await this.confirmLocation(fix, address);
    if (!okay) return null;
    return { lat: fix.lat, lng: fix.lng, accuracy: fix.accuracy };
  }

  confirmLocation(fix, address) {
    const mapUrl =
      "/api/method/tatva_connect.location.api.static_map?lat=" +
      encodeURIComponent(fix.lat) + "&lng=" + encodeURIComponent(fix.lng);
    const accTxt = fix.accuracy ? "Accuracy to " + fix.accuracy.toFixed(2) + " m" : "";
    const addrTxt = address || "Address unavailable";
    return new Promise((resolve) => {
      this.createDialog({
        title: "Location Fetched",
        html:
          '<img src="' + mapUrl + '" alt="map" style="width:100%;height:180px;object-fit:cover;' +
          "border-radius:8px;margin-bottom:12px\" onerror=\"this.style.display='none'\"/>" +
          '<div style="display:flex;gap:8px;align-items:flex-start"><span style="flex:0 0 auto;margin-top:1px">📍</span>' +
          '<div><div style="font-size:14px;line-height:1.5;color:var(--ink-gray-8)">' + ESC(addrTxt) + "</div>" +
          '<div style="color:var(--ink-gray-5);font-size:12px;margin-top:4px">' + ESC(accTxt) + "</div></div></div>",
        actions: [
          { label: "Confirm & Save", variant: "solid", onClick: (close) => { close(); resolve(true); } },
          { label: "Cancel", onClick: (close) => { close(); resolve(false); } },
        ],
      });
    });
  }

  collectActivity(type, schema) {
    ensureActivityStyle();
    const fields = schema
      .map(
        (f) =>
          '<div class="tc-af-field"><label>' + ESC(f.label) +
          (f.reqd ? ' <span class="tc-req">*</span>' : "") + "</label>" + activityControlHtml(f) + "</div>"
      )
      .join("");
    const toast = this.toast;
    return new Promise((resolve) => {
      this.createDialog({
        title: type,
        html: '<div id="tc-act-form">' + fields + "</div>",
        actions: [
          {
            label: "Save",
            variant: "solid",
            onClick: (close) => {
              const values = {};
              for (const f of schema) {
                const el = document.querySelector('#tc-act-form [data-fieldname="' + CSS.escape(f.fieldname) + '"]');
                let v = !el ? "" : f.fieldtype === "Check" ? (el.checked ? 1 : 0) : el.value;
                if (f.fieldtype === "Datetime") v = DT2LOCAL(v);
                if (f.reqd && (v === "" || v == null)) {
                  toast.error(f.label + " is required");
                  return;
                }
                values[f.fieldname] = v;
              }
              close();
              resolve(values);
            },
          },
          { label: "Cancel", onClick: (close) => { close(); resolve(null); } },
        ],
      });
    });
  }
}
