// The Tatva Connect tile's route from outside the desk (the apps screen, each app's switcher): land where the desk's own app icon lands.
frappe.pages["tatva-connect"].on_page_load = function () {
	const app = (frappe.boot.app_data || []).find((a) => a.app_name === "tatva_connect");
	const route = frappe.app.sidebar?.app_landing_route(app);
	// With no space to open, the desk would hand this page back as the route; the desk home instead.
	frappe.route_flags.replace_route = true;  // back from the space returns where the tile was clicked, not here
	frappe.set_route(route && !route.endsWith("/tatva-connect") ? route : "/desk");
};
