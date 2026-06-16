// CRM Form Script (CRM Lead, Form view) — the activity UX on the Tasks tab.
//
// Owns the whole client-side activity flow in ONE place:
//   1. A true split button [ + New Task | Log Activity ] (native New Task hidden + delegated).
//      Log Activity -> searchable type picker (grain-scoped) -> dynamic form -> save_activity.
//   2. COMPLETION: the native quick status dropdown completes a task via set_value, which never
//      runs a form-script controller — so for an ACTIVITY task we capture-phase intercept its
//      "Done" and open the activity form instead of flipping it Done empty. (Mirrors the
//      capture-phase button hijack in whatsapp_template.js.) The server `enforce_activity_logged`
//      backstop is the guarantee underneath: if this intercept ever misses, completion degrades
//      to a clear block, never a silent empty activity.
//
// Dialogs are EVENT-DRIVEN (the action button does the work, then close()) — never an awaited
// promise, so dismissing via X/Esc can't hang anything (the whatsapp_template.js pattern).
// Native $dialog + theme tokens (light/dark). DOM-based hijacks degrade safely on a crm reskin.
function setupForm({ doc, $dialog, call, createToast }) {
  const WRAP_ID = "tc-activity-split";
  const notify = (m, ok) => createToast({ message: m, type: ok ? "success" : "error" });
  const esc = (s) =>
    String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  if (!document.getElementById("tc-activity-style")) {
    const st = document.createElement("style");
    st.id = "tc-activity-style";
    st.textContent =
      "#" + WRAP_ID + "{display:inline-flex;align-items:stretch;height:28px;border-radius:8px;" +
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
      ".tc-af-field{display:flex;flex-direction:column;gap:5px;margin-bottom:12px}" +
      ".tc-af-field>label{font-size:12px;color:var(--ink-gray-6)}" +
      ".tc-af-field .tc-req{color:var(--ink-red-3)}" +
      ".tc-af-ctrl,.tc-af-search{width:100%;box-sizing:border-box;border:1px solid var(--outline-gray-2);" +
      "border-radius:8px;padding:7px 10px;font-size:13px;background:var(--surface-white);" +
      "color:var(--ink-gray-8);outline:none}.tc-af-ctrl:focus,.tc-af-search:focus{border-color:var(--outline-gray-3)}" +
      ".tc-af-search{margin-bottom:8px}textarea.tc-af-ctrl{min-height:64px;resize:vertical}";
    document.head.appendChild(st);
  }

  // ---- location capture (in-person + tracked activities, Phase B) ---------
  // Server owns the rule + radius guard (location.api). The client only reads device GPS (a
  // browser-only API). Capture happens INSIDE the Save action (event-driven), not as a separate
  // awaited dialog — the captured address is surfaced in the success toast.
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
  // Returns {} (not needed) | {lat,lng,accuracy,address} | null (denied — caller keeps form open).
  async function captureIfNeeded(type) {
    let needed = false;
    try {
      needed = await call("tatva_connect.location.api.location_needed", { lead: doc.name, task_type: type });
    } catch (e) {
      return {}; // probe failed -> server backstop decides; send no fix
    }
    if (!needed) return {};
    let fix;
    try {
      fix = await getFix();
    } catch (e) {
      notify("Location permission is required for this in-person activity.", false);
      return null;
    }
    let address = null;
    try {
      const r = await call("tatva_connect.location.api.reverse_geocode", { lat: fix.lat, lng: fix.lng });
      address = r && r.address;
    } catch (e) {}
    return { lat: fix.lat, lng: fix.lng, accuracy: fix.accuracy, address: address };
  }

  // ---- dynamic form (one renderer for both logging a new activity and completing one) -----
  const dt2local = (v) => (v && v.length === 16 ? v.replace("T", " ") + ":00" : v);

  function controlHtml(f) {
    const opts = (f.options || "").split("\n").map((o) => o.trim()).filter(Boolean);
    const a = 'class="tc-af-ctrl" data-fieldname="' + esc(f.fieldname) + '" data-fieldtype="' + esc(f.fieldtype) + '"';
    if (f.fieldtype === "Small Text") return "<textarea " + a + "></textarea>";
    if (f.fieldtype === "Select")
      return "<select " + a + '><option value=""></option>' +
        opts.map((o) => "<option>" + esc(o) + "</option>").join("") + "</select>";
    if (f.fieldtype === "Datetime") return '<input type="datetime-local" ' + a + " />";
    if (f.fieldtype === "Check") return '<input type="checkbox" ' + a + ' style="width:auto" />';
    return '<input type="text" ' + a + " />"; // Data, Link
  }

  // taskName set => complete that existing task; null => log a new ad-hoc activity.
  function openForm(type, schema, taskName) {
    const fields = schema
      .map(
        (f) =>
          '<div class="tc-af-field"><label>' + esc(f.label) +
          (f.reqd ? ' <span class="tc-req">*</span>' : "") + "</label>" + controlHtml(f) + "</div>"
      )
      .join("");
    $dialog({
      title: type,
      html: '<div id="tc-act-form">' + (fields || '<div class="tc-af-empty">No fields — saving logs this activity.</div>') + "</div>",
      actions: [
        {
          label: "Save",
          variant: "solid",
          onClick: async (close) => {
            const values = {};
            for (const f of schema) {
              const el = document.querySelector('#tc-act-form [data-fieldname="' + CSS.escape(f.fieldname) + '"]');
              let v = !el ? "" : f.fieldtype === "Check" ? (el.checked ? 1 : 0) : el.value;
              if (f.fieldtype === "Datetime") v = dt2local(v);
              if (f.reqd && (v === "" || v == null)) {
                notify(f.label + " is required", false);
                return;
              }
              values[f.fieldname] = v;
            }
            const loc = await captureIfNeeded(type);
            if (loc === null) return; // capture denied — keep the form open
            const payload = { ...values, lat: loc.lat, lng: loc.lng, accuracy: loc.accuracy };
            try {
              await call("tatva_connect.activity.api.save_activity", {
                lead: doc.name, task_type: type, values: JSON.stringify(payload),
                task: taskName || undefined,
              });
            } catch (e) {
              notify((e && e.message) || "Could not save activity", false); // incl. out-of-range block
              return;
            }
            close();
            notify("Activity logged · " + (loc.address || type), true);
            refreshOpenTasks();
          },
        },
      ],
    });
  }

  async function openForType(type, taskName) {
    let schema = [];
    try {
      schema = (await call("tatva_connect.activity.api.get_schema", { task_type: type })) || [];
    } catch (e) {
      notify("Could not load form", false);
      return;
    }
    openForm(type, schema, taskName);
  }

  // ---- type picker (Log Activity) -----------------------------------------
  async function openPicker() {
    let types = [];
    try {
      types = (await call("tatva_connect.activity.api.list_types_for_lead", { lead: doc.name })) || [];
    } catch (e) {
      notify("Could not load activity types", false);
      return;
    }
    const rows = types.length
      ? types
          .map(
            (t) =>
              '<div class="tc-af-row" data-type="' + esc(t.name) + '" data-search="' +
              esc(t.label.toLowerCase()) + '">' + esc(t.label) + "</div>"
          )
          .join("")
      : '<div class="tc-af-empty">No activity types are configured for this lead.</div>';
    $dialog({
      title: "Log Activity",
      html:
        (types.length ? '<input class="tc-af-search" type="text" placeholder="Search activity types…">' : "") +
        '<div class="tc-af-list">' + rows + "</div>",
      actions: [{ label: "Close", onClick: (close) => close() }],
    });
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
          openForType(type, null);
        });
      });
    }, 60);
  }

  // ---- complete an activity from the Tasks-tab status dropdown -------------
  // crm's quick status dropdown completes via set_value (no form-script controller). For an
  // ACTIVITY task we intercept its "Done" (capture-phase) and open the activity form instead.
  // The dropdown menu is portaled to <body> with no task identity, so we map by the row's title:
  // a click anywhere in a task row records its title; activity-task titles are unique while open
  // (one open task per type, per create_followup_task's throttle). If we can't map it, we let the
  // native flow run — the server backstop then blocks an empty completion with a clear message.
  let openTasks = [];
  function refreshOpenTasks() {
    call("tatva_connect.activity.api.open_activity_tasks", { lead: doc.name })
      .then((r) => { openTasks = r || []; })
      .catch(() => { openTasks = []; });
  }
  let lastRowTitle = null;
  const DONE_LABELS = ["Done", "Completed"];

  // Record the task row's title on any press/click inside it (covers click-open and
  // press-drag-release selection). reka-ui drives selection via a click event, so a
  // capture-phase click on the "Done" item lands BEFORE reka-ui's handler — we swallow it.
  function recordRow(e) {
    const row = e.target && e.target.closest ? e.target.closest(".activity") : null;
    if (!row) return;
    const titleEl = row.querySelector(".font-medium");
    lastRowTitle = titleEl ? (titleEl.textContent || "").trim() : null;
  }
  function onCaptureClick(e) {
    recordRow(e);
    const t = e.target;
    const item = t && t.closest ? t.closest('[role="menuitem"]') : null;
    if (!item || !lastRowTitle) return;
    if (!DONE_LABELS.includes((item.textContent || "").trim())) return;
    const task = openTasks.find((x) => x.title === lastRowTitle);
    if (!task) return; // not a known activity task -> let native run (server backstop guards)
    e.preventDefault();
    e.stopImmediatePropagation();
    openForType(task.custom_task_type, task.name);
  }
  if (window.__tcActivityCompleteHandler) {
    document.removeEventListener("click", window.__tcActivityCompleteHandler, true);
    document.removeEventListener("pointerdown", window.__tcActivityRowHandler, true);
  }
  window.__tcActivityCompleteHandler = onCaptureClick;
  window.__tcActivityRowHandler = recordRow;
  document.addEventListener("pointerdown", recordRow, true);
  document.addEventListener("click", onCaptureClick, true);
  refreshOpenTasks();

  // ---- inject the SPLIT button: [ + New Task | Log Activity ] -------------
  function findNativeNewTask() {
    return [...document.querySelectorAll("button")].find(
      (b) => (b.textContent || "").trim() === "New Task" &&
        !b.closest("#" + WRAP_ID) && !b.querySelector("input")
    );
  }
  function clickNativeNewTask() {
    const n = document.querySelector("[data-tc-native-newtask]") || findNativeNewTask();
    if (n) n.click();
  }
  function makeSeg(label, cls, onClick) {
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
  }
  function injectSplit() {
    const native = findNativeNewTask();
    if (native) {
      native.setAttribute("data-tc-native-newtask", "1");
      native.style.display = "none";
    }
    if (document.getElementById(WRAP_ID)) return;
    const anchor = native || document.querySelector("[data-tc-native-newtask]");
    if (!anchor || !anchor.parentElement) return;
    const wrap = document.createElement("div");
    wrap.id = WRAP_ID;
    wrap.appendChild(makeSeg('<span class="tc-split-plus">+</span><span>New Task</span>', "tc-split-main", clickNativeNewTask));
    wrap.appendChild(makeSeg("Log Activity", "tc-split-alt", openPicker));
    anchor.parentElement.insertBefore(wrap, anchor);
  }

  let scheduled = false;
  function reapply() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      injectSplit();
    });
  }

  if (window.__tcActivityObserver) {
    try {
      window.__tcActivityObserver.disconnect();
    } catch (e) {}
  }
  const obs = new MutationObserver(reapply);
  window.__tcActivityObserver = obs;
  obs.observe(document.body, { childList: true, subtree: true });

  injectSplit();
  [150, 500, 1200].forEach((t) => setTimeout(injectSplit, t));
}
