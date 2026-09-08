// TATVA: frappe builds its realtime redis client with no error listener, so any socket close throws and kills node — wrap the factory here, never fork frappe.
const path = require("path");

const FRAPPE = path.resolve(__dirname, "..", "..", "frappe");
const utils = require(path.join(FRAPPE, "node_utils.js"));

// Fail at boot rather than run unprotected: if frappe moves this, the wrapper is stale and silently protects nothing.
if (typeof utils.get_redis_subscriber !== "function") {
	throw new Error("TATVA: frappe node_utils.get_redis_subscriber is gone — realtime wrapper is stale, refusing to start unprotected");
}

const create = utils.get_redis_subscriber;
utils.get_redis_subscriber = function (kind, options) {
	const client = create(kind, options);
	// node-redis reconnects and re-subscribes by itself; this listener exists only so node does not throw on the way.
	client.on("error", (err) => console.error("[tatva] realtime redis:", err.message));
	return client;
};

require(path.join(FRAPPE, "socketio.js"));
