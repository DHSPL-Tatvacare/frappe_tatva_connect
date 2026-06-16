// CRM Form Script (CRM Lead, Form view) — the activity flow when a rep COMPLETES a task from the
// Tasks-tab list (moves it to "Done"). This is the ONE place the CRM gives no native hook: the list
// status dropdown calls frappe.client.set_value directly. So we capture-phase intercept just that
// "Done" click for an activity task and run the SAME flow used in the task popup: location-FIRST →
// the type's fields in a native formDialog → activity.api.save_activity (one server brain).
//
// This single interception is the only DOM-coupled piece left (no split button, no observers). It is
// FAIL-SAFE: the server validate backstop (tasks.enforce_location / enforce_activity_logged) blocks
// any unlogged in-person completion on every path, so if a CRM upgrade ever changes this markup the
// worst case is a clear "capture location to complete" toast — never a wrong or empty completion.
//
// class CRMLead runs via the CRM's native form-script lifecycle (onRender) with createDialog /
// formDialog / call / toast injected — same primitives as the task controller. Helpers are duplicated
// (each form script is evaluated in its own scope; there is no shared module to import).

const TCL_DONE = "Done";
const tclEsc = (s) =>
  String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const tclMsg = (e) => (e && (e.messages?.[0] || e.message)) || "";

function tclGetGPS() {
  return new Promise((resolve) => {
    if (!navigator.geolocation) return resolve(null);
    navigator.geolocation.getCurrentPosition(
      (p) => resolve({ lat: p.coords.latitude, lng: p.coords.longitude, accuracy: p.coords.accuracy }),
      () => resolve(null),
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 }
    );
  });
}

function tclBuildFields(schema) {
  const fields = [{ fieldname: "notes", label: "Notes", fieldtype: "Small Text" }];
  for (const f of schema) {
    fields.push({
      fieldname: f.fieldname,
      label: f.label,
      fieldtype: f.fieldtype,
      options: f.options || undefined,
      reqd: f.reqd ? 1 : 0,
      depends_on: f.depends_on || undefined,
    });
  }
  return fields;
}

function tclStaticMap(lat, lng, here) {
  let u = "/api/method/tatva_connect.location.api.static_map?lat=" + encodeURIComponent(lat) + "&lng=" + encodeURIComponent(lng);
  if (here) u += "&here_lat=" + encodeURIComponent(here.lat) + "&here_lng=" + encodeURIComponent(here.lng);
  return u;
}

async function tclLocationFirst(ctl, lead, type) {
  let needed = false;
  try {
    needed = await ctl.call("tatva_connect.location.api.location_needed", { lead, task_type: type });
  } catch (e) {
    return null;
  }
  if (!needed) return null;
  const pos = await tclGetGPS();
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
    ctl.createDialog({
      title: "Too far from the doctor",
      html:
        '<img src="' + tclStaticMap(pre.anchor_lat, pre.anchor_lng, pos) + '" alt="map" ' +
        "style=\"width:100%;height:180px;object-fit:cover;border-radius:8px;margin-bottom:12px\" onerror=\"this.style.display='none'\"/>" +
        '<div style="font-size:14px;color:var(--ink-gray-8)">Reach within <b>' + pre.allowed_m +
        " m</b> of the doctor's location to log this visit. You're <b>" + pre.distance_m + ' m</b> away.</div>' +
        (pre.anchor_address
          ? '<div style="display:flex;gap:6px;margin-top:10px;color:var(--ink-gray-6);font-size:12px">📍 ' +
            tclEsc(pre.anchor_address) + "</div>"
          : ""),
      actions: [{ label: "Okay", variant: "solid", onClick: (c) => c() }],
    });
    ctl.toast.error("You're " + pre.distance_m + " m away — too far to log this visit.");
    return "blocked";
  }
  return { fix: { lat: pos.lat, lng: pos.lng, accuracy: pos.accuracy } };
}

function tclShowReceipt(ctl, fix, type) {
  const acc = fix.accuracy ? "±" + Math.round(fix.accuracy) + " m" : "";
  ctl.createDialog({
    title: "Visit location captured",
    html:
      '<img src="' + tclStaticMap(fix.lat, fix.lng) + '" alt="map" ' +
      "style=\"width:100%;height:180px;object-fit:cover;border-radius:8px;margin-bottom:12px\" onerror=\"this.style.display='none'\"/>" +
      '<div style="font-size:14px;color:var(--ink-gray-8)">' + tclEsc(type) + " logged at your current location." +
      (acc ? ' <span style="color:var(--ink-gray-5);font-size:12px">(' + acc + ")</span>" : "") + "</div>",
    actions: [{ label: "Done", variant: "solid", onClick: (c) => c() }],
  });
}

class CRMLead {
  onRender() {
    this._tcLead = this.doc && this.doc.name;
    if (!this._tcLead) return;
    this._tcSetupCompletionIntercept();
  }

  // Refreshable cache of this lead's OPEN activity tasks, so the capture-phase handler can decide
  // synchronously (before the native set_value) whether a "Done" click belongs to an activity.
  _tcRefreshOpenTasks() {
    this.call("tatva_connect.activity.api.open_activity_tasks", { lead: this._tcLead })
      .then((r) => { this._tcOpen = r || []; })
      .catch(() => { this._tcOpen = []; });
  }

  _tcSetupCompletionIntercept() {
    this._tcOpen = this._tcOpen || [];
    this._tcRefreshOpenTasks();
    const ctl = this;

    // Record the task row's title on any pointer interaction inside it (the status dropdown menu is
    // portaled to <body> with no task identity, so we map the later "Done" click by the row title —
    // unique among a lead's open activity tasks, one open per type).
    const recordRow = (e) => {
      const row = e.target && e.target.closest ? e.target.closest(".activity") : null;
      if (!row) return;
      const t = row.querySelector(".font-medium");
      ctl._tcRow = t ? (t.textContent || "").trim() : null;
    };
    const onClick = (e) => {
      const el = e.target;
      const item = el && el.closest ? el.closest('[role="menuitem"],button,a,li') : null;
      if (!item || (item.textContent || "").trim() !== TCL_DONE) return;
      const title = ctl._tcRow;
      if (!title) return;
      const task = (ctl._tcOpen || []).find((x) => x.title === title);
      if (!task) return; // not a known open activity task → let native completion run (backstop guards)
      e.preventDefault();
      e.stopImmediatePropagation();
      ctl._tcCompleteFromList(task);
    };

    if (window.__tclRowHandler) document.removeEventListener("pointerdown", window.__tclRowHandler, true);
    if (window.__tclClickHandler) document.removeEventListener("click", window.__tclClickHandler, true);
    window.__tclRowHandler = recordRow;
    window.__tclClickHandler = onClick;
    document.addEventListener("pointerdown", recordRow, true);
    document.addEventListener("click", onClick, true);
  }

  async _tcCompleteFromList(task) {
    const lead = this._tcLead;
    const type = task.custom_task_type;
    let schema;
    try {
      schema = (await this.call("tatva_connect.activity.api.get_schema", { task_type: type })) || [];
    } catch (e) {
      this.toast.error("Couldn't load this activity — please try again.");
      return;
    }
    const loc = await tclLocationFirst(this, lead, type);
    if (loc === "denied" || loc === "blocked") return; // toast/dialog already shown

    const fix = loc && loc.fix;
    const data = await this.formDialog({
      title: "Add " + type,
      fields: tclBuildFields(schema),
      submitLabel: "Submit Activity",
      cancelLabel: "Cancel",
    });
    if (data === null || data === undefined) return; // cancelled

    const values = Object.assign({}, data);
    if (fix) Object.assign(values, { lat: fix.lat, lng: fix.lng, accuracy: fix.accuracy });
    try {
      await this.call("tatva_connect.activity.api.save_activity", {
        lead, task_type: type, values: JSON.stringify(values), task: task.name,
      });
    } catch (e) {
      this.toast.error(tclMsg(e) || "Couldn't save this activity — please try again.");
      return;
    }
    this.toast.success(type + " completed.");
    if (fix) tclShowReceipt(this, fix, type);
    this._tcRefreshOpenTasks();
    // Nudge the Tasks list to re-fetch (the native reload path was intercepted). Verified live.
    if (window.__tclReload) window.__tclReload();
  }
}
