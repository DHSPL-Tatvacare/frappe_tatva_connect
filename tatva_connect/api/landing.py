"""Landing-page audit — read-only diagnostics for where Desk users land on login.

Frappe's native login redirect (frappe/auth.py -> frappe/apps.py:get_default_path)
ALREADY implements the house rule, no fork needed:
  * exactly one visible app   -> that app's route (e.g. /crm)
  * more than one visible app -> /apps (the app switcher)
  * zero visible web apps      -> /app (Desk)
  * a User.default_workspace / User.default_app / System Settings.default_app, if
    set, OVERRIDES the above and pins the user to one place.

So "sales user with one app lands on CRM, manager with many lands on /apps, Desk
reachable via the switcher" is configuration (which app-roles a user carries +
keeping the override fields blank), not code. This endpoint only SHOWS, per user,
what they will see and the reason — so an admin can spot e.g. a sales user who lands
on /apps only because the auto-assigned `LMS Student` role inflated their app count,
or a stray `default_workspace`. It is strictly read-only; it changes nothing.
"""
import frappe
from frappe import _
from frappe.utils import slug


def _compute_landing(user):
    """Faithfully replay the framework's System-User landing resolution for `user`.
    Must be called with frappe.session.user already set to `user` (get_default_path
    reads the session)."""
    from frappe.apps import get_default_path

    dw = frappe.db.get_value("User", user, "default_workspace")
    if dw:
        return "/app/" + slug(dw)
    return get_default_path() or "/app"


def _reason(apps, default_app, default_workspace):
    if default_workspace:
        return "pinned to Desk workspace '{0}' by User.default_workspace".format(default_workspace)
    if default_app:
        return "pinned to '{0}' by User.default_app".format(default_app)
    if not apps:
        return "no web apps visible -> Desk (/app)"
    if len(apps) == 1:
        return "single app -> {0}".format(apps[0])
    return "multiple apps {0} -> /apps (app switcher)".format(apps)


@frappe.whitelist()
def audit(user=None):
    """System-Manager-only, READ-ONLY. For one `user` or every enabled System User:
    the apps they can see, the path login will land them on, the override fields, and
    a plain-English reason. Use it to find users mis-landing and why."""
    frappe.only_for("System Manager")
    from frappe.apps import get_apps

    if user:
        users = [user]
    else:
        users = frappe.get_all(
            "User", filters={"enabled": 1, "user_type": "System User"}, pluck="name"
        )

    current = frappe.session.user
    rows = []
    try:
        for u in users:
            frappe.set_user(u)
            apps = [a.get("route") for a in get_apps()]
            da = frappe.db.get_value("User", u, "default_app")
            dw = frappe.db.get_value("User", u, "default_workspace")
            rows.append({
                "user": u,
                "apps": apps,
                "lands_on": _compute_landing(u),
                "default_app": da,
                "default_workspace": dw,
                "reason": _reason(apps, da, dw),
            })
    finally:
        frappe.set_user(current)

    return {
        "system_default_app": frappe.db.get_single_value("System Settings", "default_app"),
        "rule": "1 app -> that app; >1 app -> /apps; overrides (default_app/workspace) win",
        "count": len(rows),
        "users": rows,
    }
