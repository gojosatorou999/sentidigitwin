/**
 * SSE client with a 60s poll fallback (README section 8.6).
 *
 * `TwinStream.connect({city, zone, onStateUpdate, onIncident, onStatusChange})`
 * returns a handle with `.close()`. Falls back to polling automatically if
 * `EventSource` is unavailable or the connection errors out repeatedly --
 * the frontend must keep working even when SSE itself is the thing that's
 * degraded (same C1 spirit as the backend adapters).
 */

(function (global) {
  "use strict";

  const POLL_INTERVAL_MS = 60000;
  const MAX_RECONNECT_ATTEMPTS = 3;

  function connect(options) {
    const { city, zone, onStateUpdate, onIncident, onStatusChange, onPollTick } = options;
    let closed = false;
    let source = null;
    let reconnectAttempts = 0;
    let pollTimer = null;

    function emitStatus(status) {
      if (onStatusChange) onStatusChange(status);
    }

    function startPolling() {
      if (pollTimer) return;
      emitStatus("polling");
      pollTimer = setInterval(() => {
        if (onPollTick) onPollTick();
      }, POLL_INTERVAL_MS);
      if (onPollTick) onPollTick(); // immediate first tick
    }

    function stopPolling() {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    }

    function startSSE() {
      if (typeof EventSource === "undefined") {
        startPolling();
        return;
      }

      const params = new URLSearchParams();
      if (city) params.set("city", city);
      if (zone) params.set("zone", zone);
      const url = "/api/twin/stream" + (params.toString() ? "?" + params.toString() : "");

      source = new EventSource(url);

      source.addEventListener("state_update", (evt) => {
        stopPolling();
        emitStatus("live");
        reconnectAttempts = 0;
        try {
          const data = JSON.parse(evt.data);
          if (onStateUpdate) onStateUpdate(data);
        } catch (err) {
          console.warn("twin-stream: malformed state_update", err);
        }
      });

      source.addEventListener("incident", (evt) => {
        try {
          const data = JSON.parse(evt.data);
          if (onIncident) onIncident(data);
        } catch (err) {
          console.warn("twin-stream: malformed incident event", err);
        }
      });

      source.onopen = () => {
        stopPolling();
        emitStatus("live");
        reconnectAttempts = 0;
      };

      source.onerror = () => {
        if (closed) return;
        reconnectAttempts += 1;
        if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
          emitStatus("degraded");
          if (source) {
            source.close();
            source = null;
          }
          startPolling();
        }
      };
    }

    startSSE();

    return {
      close() {
        closed = true;
        stopPolling();
        if (source) {
          source.close();
          source = null;
        }
      },
    };
  }

  global.TwinStream = { connect };
})(window);
