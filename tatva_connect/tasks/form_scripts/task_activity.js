// CRM Form Script (CRM Task, Form view) — the activity flow inside the task popup.
//
// Native lifecycle only (no DOM hacks): the CRM injects createDialog / formDialog / call / toast /
// throwError onto the controller and fires onBeforeCreate (new task) + onValidate (completing one).
// Both run the SAME flow: location-FIRST (capture GPS, check the doctor anchor, block if too far) →
// the activity type's own fields in a native formDialog → compute the CRM Task field values on the
// server (one brain: activity.api.compute_activity) → STAMP them onto this.doc and let the native
// save persist ONCE (no double-insert, no lingering popup). Clear toasts on every outcome.
//
// The server validate backstop (tasks.enforce_location / enforce_activity_logged) guarantees an
// in-person activity can never be saved Done without an in-range fix on ANY path — this is UX on top.

const TC_ABORT = "__tc_activity_pending__"; // a throw the CRM's save wrapper swallows silently
const tcEsc = (s) =>
  String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const tcMsg = (e) => (e && (e.messages?.[0] || e.message)) || "";

function tcGetGPS() {
  return new Promise((resolve) => {
    if (!navigator.geolocation) return resolve(null);
    navigator.geolocation.getCurrentPosition(
      (p) => resolve({ lat: p.coords.latitude, lng: p.coords.longitude, accuracy: p.coords.accuracy }),
      () => resolve(null),
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 }
    );
  });
}

// schema rows (activity.api.get_schema) -> native frappe-ui field defs, with a leading Notes field.
function tcBuildFields(schema) {
  const fields = [{ fieldname: "notes", label: "Notes", fieldtype: "Small Text" }];
  for (const f of schema) {
    fields.push({
      fieldname: f.fieldname,
      label: f.label,
      fieldtype: f.fieldtype, // Data / Small Text / Select / Datetime / Check / Link
      options: f.options || undefined, // Select: newline opts · Link: target doctype
      reqd: f.reqd ? 1 : 0,
      depends_on: f.depends_on || undefined,
    });
  }
  return fields;
}

function tcStaticMap(lat, lng, here) {
  let u = "/api/method/tatva_connect.location.api.static_map?lat=" + encodeURIComponent(lat) + "&lng=" + encodeURIComponent(lng);
  if (here) u += "&here_lat=" + encodeURIComponent(here.lat) + "&here_lng=" + encodeURIComponent(here.lng);
  return u;
}

// LOCATION-FIRST gate. Returns: null (not needed) | {fix} (ok) | "denied" | "blocked".
// Shows the right toast/dialog itself; the caller only decides whether to proceed.
async function tcLocationFirst(ctl, lead, type) {
  let needed = false;
  try {
    needed = await ctl.call("tatva_connect.location.api.location_needed", { lead, task_type: type });
  } catch (e) {
    return null; // probe failed → compute re-checks server-side; don't ask GPS here
  }
  if (!needed) return null;
  const pos = await tcGetGPS();
  if (!pos) {
    ctl.toast.error("Allow location access to log this in-person visit.");
    return "denied";
  }
  let pre;
  try {
    pre = await ctl.call("tatva_connect.location.api.precheck", {
      lead, task_type: type, lat: pos.lat, lng: pos.lng, accuracy: pos.accuracy,
    });
  } catch (e) {
    ctl.toast.error("Couldn't verify your location — please try again.");
    return "denied";
  }
  if (pre.needed && pre.ok === false) {
    tcShowBlock(ctl, pre, pos);
    ctl.toast.error("You're " + pre.distance_m + " m away — too far to log this visit.");
    return "blocked";
  }
  return { fix: { lat: pos.lat, lng: pos.lng, accuracy: pos.accuracy } };
}

function tcShowBlock(ctl, pre, pos) {
  ctl.createDialog({
    title: "Too far from the doctor",
    html:
      '<img src="' + tcStaticMap(pre.anchor_lat, pre.anchor_lng, pos) + '" alt="map" ' +
      "style=\"width:100%;height:180px;object-fit:cover;border-radius:8px;margin-bottom:12px\" onerror=\"this.style.display='none'\"/>" +
      '<div style="font-size:14px;color:var(--ink-gray-8)">Reach within <b>' + pre.allowed_m +
      " m</b> of the doctor's location to log this visit. You're <b>" + pre.distance_m + ' m</b> away.</div>' +
      (pre.anchor_address
        ? '<div style="display:flex;gap:6px;margin-top:10px;color:var(--ink-gray-6);font-size:12px">📍 ' +
          tcEsc(pre.anchor_address) + "</div>"
        : ""),
    actions: [{ label: "Okay", variant: "solid", onClick: (c) => c() }],
  });
}

function tcShowReceipt(ctl, fix, type) {
  const acc = fix.accuracy ? "±" + Math.round(fix.accuracy) + " m" : "";
  ctl.createDialog({
    title: "Visit location captured",
    html:
      '<img src="' + tcStaticMap(fix.lat, fix.lng) + '" alt="map" ' +
      "style=\"width:100%;height:180px;object-fit:cover;border-radius:8px;margin-bottom:12px\" onerror=\"this.style.display='none'\"/>" +
      '<div style="font-size:14px;color:var(--ink-gray-8)">' + tcEsc(type) + " logged at your current location." +
      (acc ? ' <span style="color:var(--ink-gray-5);font-size:12px">(' + acc + ")</span>" : "") + "</div>",
    actions: [{ label: "Done", variant: "solid", onClick: (c) => c() }],
  });
}

// The shared popup flow: gate location, collect the type's fields, compute, stamp onto the doc.
// Returns true to let the native save proceed, or throws TC_ABORT to cancel it (silently).
async function tcRunPopupActivity(ctl) {
  const doc = ctl.doc;
  if (!doc || doc.reference_doctype !== "CRM Lead" || !doc.reference_docname || !doc.custom_task_type) return false;
  const lead = doc.reference_docname;
  const type = doc.custom_task_type;

  let schema = [];
  try {
    schema = (await ctl.call("tatva_connect.activity.api.get_schema", { task_type: type })) || [];
  } catch (e) {
    return false; // can't resolve → plain task, let native proceed (server backstop guards)
  }

  // Location-FIRST, before deciding whether to open a form: an In-Person tracked type with NO schema
  // fields still needs its GPS captured (else the backstop blocks completion). Mirrors the list path.
  const loc = await tcLocationFirst(ctl, lead, type);
  if (loc === "denied" || loc === "blocked") throw new Error(TC_ABORT); // toast already shown
  if (!schema.length && loc === null) return false; // plain task, nothing to capture → native save
  const fix = loc && loc.fix;

  const data = await ctl.formDialog({
    title: "Add " + type,
    fields: tcBuildFields(schema),
    submitLabel: "Submit Activity",
    cancelLabel: "Cancel",
  });
  if (data === null || data === undefined) throw new Error(TC_ABORT); // cancelled — don't complete

  const values = Object.assign({}, data);
  if (fix) Object.assign(values, { lat: fix.lat, lng: fix.lng, accuracy: fix.accuracy });

  let fields;
  try {
    fields = await ctl.call("tatva_connect.activity.api.compute_activity_fields", {
      lead, task_type: type, values: JSON.stringify(values),
    });
  } catch (e) {
    ctl.toast.error(tcMsg(e) || "Couldn't save this activity — please try again.");
    throw new Error(TC_ABORT);
  }

  Object.assign(doc, fields);
  if (!doc.title) doc.title = type;
  ctl.toast.success(type + " logged.");
  if (fix) tcShowReceipt(ctl, fix, type);
  return true; // let the native create/save persist the enriched doc once
}

class CRMTask {
  // New task whose type is an activity → run the flow, stamp the doc, then native create saves once.
  async onBeforeCreate() {
    await tcRunPopupActivity(this);
  }

  // Completing an activity from the task popup (status → Done) → same flow, then native save persists.
  async onValidate() {
    const doc = this.doc;
    if (!doc || (doc.status || "") !== "Done") return;
    if (doc.custom_activity_payload) return; // already logged
    await tcRunPopupActivity(this);
  }
}
