// TATVA: tatva_connect's socket handlers; frappe's realtime server loads apps/<app>/realtime/handlers.js for every installed app on each connection (frappe realtime/index.js).
const REPORT = "/api/method/tatva_connect.presence.report";
const STATES = ["active", "idle", "hidden"];

// One tab's presence: forwarded to Python only when it changes, keyed by this socket's id.
module.exports = function tatva_handlers(socket) {
	socket.on("tatva_presence", (data) => {
		const state = data && data.state;
		if (!STATES.includes(state)) return;
		// Remembered so a disconnect is reported only for a tab that said it was in use; a hidden tab's drop costs nothing.
		socket.tatva_presence = state === "hidden" ? null : state;
		report(socket, state, data.idle_for);
	});
	socket.on("disconnect", () => {
		if (socket.tatva_presence) report(socket, "gone");
	});
};

function report(socket, state, idle_for) {
	const args = { state, tab: socket.id };
	if (Number.isFinite(idle_for)) args.idle_for = idle_for;
	// A lost report heals on the tab's next change or reconnect, so there is nothing to retry.
	socket.frappe_request(REPORT, args).catch((err) => console.error("[tatva] presence report:", err.message));
}
