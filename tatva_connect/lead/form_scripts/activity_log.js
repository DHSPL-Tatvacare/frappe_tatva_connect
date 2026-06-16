// CRM Form Script (CRM Lead, Form view) — the activity UX on the Tasks tab. TWO entry points, ONE flow:
//
//   1. LOG ACTIVITY (picker): a split button [ + New Task | Log Activity ] is injected next to the
//      native "New Task". "New Task" delegates to native; "Log Activity" opens a grain-scoped,
//      searchable type picker → the chosen type runs the activity flow as an ad-hoc new task.
//   2. COMPLETE (list "Done"): the CRM list status dropdown calls frappe.client.set_value directly —
//      no native hook — so we capture-phase intercept the "Done" click for an activity task and run
//      the SAME flow against the existing task instead of an empty status flip.
//
// Both paths funnel through ONE method, `_tcRunActivity({type, taskName})` — taskName set ⇒ complete
// that task, null ⇒ log a new ad-hoc activity. The flow is: location-FIRST (door block for pure
// in-person) → the type's fields in a native formDialog → conditional at-form location check (for
// `location_when` types that only become a visit once a branch field is picked) → save_activity (one
// server brain) → receipt + toast.
//
// FAIL-SAFE: the server validate backstop (tasks.enforce_location / enforce_activity_logged) blocks
// any unlogged or out-of-range in-person completion on EVERY path. So the DOM-coupled pieces (the
// "Done" intercept and the best-effort split-button injection) degrade safely — if a CRM upgrade ever
// changes the markup, the worst case is the rep falls back to native New Task / a clear block toast,
// never a wrong or empty completion.
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

const TCL_WRAP_ID = "tc-activity-split";

class CRMLead {
  onRender() {
    this._tcLead = this.doc && this.doc.name;
    if (!this._tcLead) return;
    this._tcSetupCompletionIntercept();
    this._tcInjectStyle();
    this._tcSetupSplitButton();
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

  // List-"Done" path: complete the existing task. Thin wrapper over the one shared flow.
  _tcCompleteFromList(task) {
    return this._tcRunActivity({ type: task.custom_task_type, taskName: task.name });
  }

  // THE one client flow both entry points call (list-Done and the Log-Activity picker). taskName set
  // ⇒ complete that task; null ⇒ log a new ad-hoc activity. Order: load schema → location-FIRST (door
  // block for pure in-person) → formDialog → conditional at-form location (location_when types) →
  // save_activity → receipt + toast. Every outcome clears with a toast (denied / out-of-range /
  // save-fail / success).
  async _tcRunActivity({ type, taskName }) {
    const lead = this._tcLead;
    let schema;
    try {
      schema = (await this.call("tatva_connect.activity.api.get_schema", { task_type: type })) || [];
    } catch (e) {
      this.toast.error("Couldn't load this activity — please try again.");
      return;
    }

    // Fill the form, THEN resolve location from the chosen values (Phone Call → none; Field Visit → check).
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
    this._tcRefreshOpenTasks();
    // Nudge the Tasks list to re-fetch (the native reload path was intercepted). Verified live.
    if (window.__tclReload) window.__tclReload();
  }

  // ---- Log Activity: grain-scoped, searchable type picker ----------------
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
        '<div class="tc-af-list">' + rows + "</div>",
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

  // ---- split button: [ + New Task | Log Activity ] -----------------------
  // The native "New Task" button has no stable hook, so this is a contained, best-effort DOM
  // injection: find it by visible text, hide it, insert the split wrapper before it. Idempotent
  // (guarded by the wrapper id). If it ever fails to render, the rep falls back to native New Task —
  // correctness is server-enforced, so this UI is convenience only.
  _tcInjectStyle() {
    if (document.getElementById("tc-activity-style")) return;
    const st = document.createElement("style");
    st.id = "tc-activity-style";
    st.textContent =
      "#" + TCL_WRAP_ID + "{display:inline-flex;align-items:stretch;height:28px;border-radius:8px;" +
      "overflow:hidden;border:1px solid var(--outline-gray-2)}" +
      ".tc-split-seg{display:inline-flex;align-items:center;gap:6px;height:100%;padding:0 12px;border:none;" +
      "background:var(--surface-white);color:var(--ink-gray-8);font-size:13px;font-weight:500;" +
      "cursor:pointer;white-space:nowrap}" +
      ".tc-split-seg:hover{background:var(--surface-gray-2)}" +
      ".tc-split-alt{border-left:1px solid var(--outline-gray-2)}" +
      ".tc-split-plus{font-size:15px;line-height:1}" +
      ".tc-af-list{display:flex;flex-direction:column;gap:2px;max-height:50vh;overflow:auto}" +
      ".tc-af-row{display:flex;align-items:center;gap:8px;padding:9px 10px;border-radius:8px;cursor:pointer;" +
      "font-size:13px;color:var(--ink-gray-8)}.tc-af-row:hover{background:var(--surface-gray-2)}" +
      ".tc-af-empty{padding:18px;text-align:center;color:var(--ink-gray-5);font-size:13px}" +
      ".tc-af-search{width:100%;box-sizing:border-box;border:1px solid var(--outline-gray-2);border-radius:8px;" +
      "padding:7px 10px;font-size:13px;background:var(--surface-white);color:var(--ink-gray-8);outline:none;" +
      "margin-bottom:8px}.tc-af-search:focus{border-color:var(--outline-gray-3)}";
    document.head.appendChild(st);
  }

  _tcFindNativeNewTask() {
    return [...document.querySelectorAll("button")].find(
      (b) => (b.textContent || "").trim() === "New Task" &&
        !b.closest("#" + TCL_WRAP_ID) && !b.querySelector("input")
    );
  }

  _tcInjectSplit() {
    const native = this._tcFindNativeNewTask();
    if (native) {
      native.setAttribute("data-tc-native-newtask", "1");
      native.style.display = "none";
    }
    if (document.getElementById(TCL_WRAP_ID)) return; // already injected — idempotent
    const anchor = native || document.querySelector("[data-tc-native-newtask]");
    if (!anchor || !anchor.parentElement) return;
    const ctl = this;
    const seg = (label, cls, onClick) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "tc-split-seg " + cls;
      b.innerHTML = label;
      b.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        onClick();
      });
      return b;
    };
    const wrap = document.createElement("div");
    wrap.id = TCL_WRAP_ID;
    wrap.appendChild(
      seg('<span class="tc-split-plus">+</span><span>New Task</span>', "tc-split-main", () => {
        const n = document.querySelector("[data-tc-native-newtask]") || ctl._tcFindNativeNewTask();
        if (n) n.click();
      })
    );
    wrap.appendChild(seg("Log Activity", "tc-split-alt", () => ctl.openPicker()));
    anchor.parentElement.insertBefore(wrap, anchor);
  }

  // The Tasks tab mounts after onRender and re-renders on tab switches, so a single inject can miss.
  // A minimal MutationObserver (rAF-coalesced, single handler parked on window, idempotent via the
  // wrapper id) reapplies the injection. Plus a few timed retries for the first mount.
  _tcSetupSplitButton() {
    const ctl = this;
    let scheduled = false;
    const reapply = () => {
      if (scheduled) return;
      scheduled = true;
      requestAnimationFrame(() => {
        scheduled = false;
        ctl._tcInjectSplit();
      });
    };
    if (window.__tclSplitObserver) {
      try { window.__tclSplitObserver.disconnect(); } catch (e) {}
    }
    const obs = new MutationObserver(reapply);
    window.__tclSplitObserver = obs;
    obs.observe(document.body, { childList: true, subtree: true });
    this._tcInjectSplit();
    [150, 500, 1200].forEach((t) => setTimeout(() => ctl._tcInjectSplit(), t));
  }
}
