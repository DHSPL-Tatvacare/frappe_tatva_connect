// CRM Form Script (CRM Lead, Form view) — the ad-hoc "Log Activity" flow.
//
// The native CRM Tasks header (fork: ActivityHeader.vue) renders a split button [ New Task | ▾ Log
// Activity ]. Its "Log Activity" item calls window.__tcLogActivity(), which this script sets to open a
// grain-scoped, searchable activity-type picker. Picking a type runs the flow: fill the type's fields
// in a native formDialog → resolve location from the chosen values (Phone Call → none; Field Visit →
// check the doctor anchor, block if too far) → save_activity → receipt + toast → refresh the native
// board (window.__tcReloadTasks, exposed by TatvaTasks).
//
// NO DOM hacks: the task list, cards, completion and detail are owned by the native TatvaTasks board in
// the fork. This script is only the ad-hoc punch entry point, bridged to the native button via window.
// FAIL-SAFE: the server validate backstop (tasks.enforce_location / enforce_activity_logged) still
// guards every completion on every path — this UX sits on top.

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

// THE one location step (post-form): does THIS submission need a location? If so capture GPS + check
// the doctor anchor. Returns null (not needed) | {fix} | "denied" | "blocked". precheck logs any
// rejection itself; this just surfaces the dialog/toast.
async function tclResolveLocation(ctl, lead, type, values) {
  let needed = false;
  try {
    needed = await ctl.call("tatva_connect.location.api.location_needed", {
      lead, task_type: type, values: JSON.stringify(values),
    });
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
      lead, task_type: type, lat: pos.lat, lng: pos.lng, accuracy: pos.accuracy, values: JSON.stringify(values),
    });
  } catch (e) {
    ctl.toast.error("Couldn't verify your location — please try again.");
    return "denied";
  }
  if (pre.ok === false) {
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
  // Expose the ad-hoc picker for the native split button (ActivityHeader → window.__tcLogActivity).
  onRender() {
    this._tcLead = this.doc && this.doc.name;
    if (!this._tcLead) return;
    const ctl = this;
    window.__tcLogActivity = () => ctl.openPicker();
  }

  // THE ad-hoc activity flow: fill the type's fields in a formDialog → resolve location from the chosen
  // values (Phone → none; Field Visit → check the doctor anchor) → save_activity → receipt + toast.
  // GUARDED: one flow at a time (window.__tcFlowBusy) so slow GPS + repeat taps can NEVER stack forms.
  async _tcRunActivity({ type, taskName }) {
    if (window.__tcFlowBusy) return;
    window.__tcFlowBusy = true;
    try {
      const lead = this._tcLead;
      let schema;
      try {
        schema = (await this.call("tatva_connect.activity.api.get_schema", { task_type: type })) || [];
      } catch (e) {
        this.toast.error("Couldn't load this activity — please try again.");
        return;
      }

      const data = await this.formDialog({
        title: "Add " + type,
        fields: tclBuildFields(schema),
        submitLabel: "Submit Activity",
        cancelLabel: "Cancel",
      });
      if (data === null || data === undefined) return; // cancelled

      const values = Object.assign({}, data);
      const loc = await tclResolveLocation(this, lead, type, values);
      if (loc === "denied" || loc === "blocked") return; // toast/dialog already shown
      const fix = loc && loc.fix;
      if (fix) Object.assign(values, { lat: fix.lat, lng: fix.lng, accuracy: fix.accuracy });

      try {
        await this.call("tatva_connect.activity.api.save_activity", {
          lead, task_type: type, values: JSON.stringify(values), task: taskName || undefined,
        });
      } catch (e) {
        this.toast.error(tclMsg(e) || "Couldn't save this activity — please try again.");
        return;
      }
      this.toast.success(taskName ? type + " completed." : type + " logged.");
      if (fix) tclShowReceipt(this, fix, type);
      if (window.__tcReloadTasks) window.__tcReloadTasks(); // refresh the native board
    } finally {
      window.__tcFlowBusy = false;
    }
  }

  // Grain-scoped, searchable activity-type picker (the ad-hoc entry point).
  async openPicker() {
    let types;
    try {
      types = (await this.call("tatva_connect.activity.api.list_types_for_lead", { lead: this._tcLead })) || [];
    } catch (e) {
      this.toast.error("Couldn't load activity types — please try again.");
      return;
    }
    const ctl = this;
    const rows = types.length
      ? types
          .map(
            (t) =>
              '<div class="tc-af-row" data-type="' + tclEsc(t.name) + '" data-search="' +
              tclEsc(String(t.label || t.name).toLowerCase()) + '">' + tclEsc(t.label || t.name) + "</div>"
          )
          .join("")
      : '<div class="tc-af-empty">No activity types are configured for this lead.</div>';
    this.createDialog({
      title: "Log Activity",
      html:
        (types.length ? '<input class="tc-af-search" type="text" placeholder="Search activity types…">' : "") +
        '<div class="tc-af-list">' + rows + "</div>" +
        "<style>" +
        ".tc-af-list{display:flex;flex-direction:column;gap:2px;max-height:50vh;overflow:auto}" +
        ".tc-af-row{display:flex;align-items:center;gap:8px;padding:9px 10px;border-radius:8px;cursor:pointer;" +
        "font-size:13px;color:var(--ink-gray-8)}.tc-af-row:hover{background:var(--surface-gray-2)}" +
        ".tc-af-empty{padding:18px;text-align:center;color:var(--ink-gray-5);font-size:13px}" +
        ".tc-af-search{width:100%;box-sizing:border-box;border:1px solid var(--outline-gray-2);border-radius:8px;" +
        "padding:7px 10px;font-size:13px;background:var(--surface-white);color:var(--ink-gray-8);outline:none;" +
        "margin-bottom:8px}.tc-af-search:focus{border-color:var(--outline-gray-3)}" +
        "</style>",
      actions: [{ label: "Close", onClick: (close) => close() }],
    });
    // Wire the filter + row clicks once the dialog DOM is mounted.
    setTimeout(() => {
      const box = document.querySelector(".tc-af-search");
      if (box) {
        box.focus();
        box.addEventListener("input", () => {
          const q = box.value.trim().toLowerCase();
          document.querySelectorAll(".tc-af-row").forEach((el) => {
            el.style.display = !q || (el.getAttribute("data-search") || "").indexOf(q) !== -1 ? "" : "none";
          });
        });
      }
      document.querySelectorAll(".tc-af-row").forEach((el) => {
        el.addEventListener("click", () => {
          const type = el.getAttribute("data-type");
          const closeBtn = [...document.querySelectorAll('[role="dialog"] button')].find(
            (b) => (b.textContent || "").trim() === "Close"
          );
          if (closeBtn) closeBtn.click();
          ctl._tcRunActivity({ type, taskName: null });
        });
      });
    }, 60);
  }
}
