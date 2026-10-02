// Injected at the top of every canvas document. Runs inside the sandboxed,
// opaque-origin iframe; its only channel to the host is postMessage.
(function () {
  "use strict";
  const boot = window.__BRIDGE_BOOT;
  delete window.__BRIDGE_BOOT;

  const post = (msg) => parent.postMessage({ ...msg, version: boot.version }, "*");
  const listeners = new Set();
  const history = [];
  let collecting = null; // errors raised while applying a patch
  let startup = []; // errors raised before the "ready" report; null afterwards

  function report(message) {
    if (collecting) collecting.push(message);
    else if (startup) startup.push(message);
    else post({ kind: "error", errors: [message] });
  }

  window.addEventListener("error", (e) => {
    const line = e.lineno && e.filename === location.href ? e.lineno - boot.lineOffset : e.lineno;
    report(`${e.message}${line ? ` (line ${line})` : ""}`);
  });
  document.addEventListener("securitypolicyviolation", (e) => {
    report(`Blocked by canvas policy (${e.effectiveDirective}): ${e.blockedURI || "inline"}`);
  });
  window.addEventListener("unhandledrejection", (e) => {
    report(`Unhandled rejection: ${e.reason && e.reason.message || e.reason}`);
  });

  // createContextualFragment, unlike innerHTML, makes inserted <script> tags run.
  function fragment(html) {
    return document.createRange().createContextualFragment(html || "");
  }

  // ---- slots: named regions of a layout that `update` fills with data ----

  const renderers = new Map(); // slot name -> fn(value, {slot, key, el})

  function slotElement(name) {
    return document.querySelector(`[data-slot="${CSS.escape(name)}"]`);
  }

  function listSlots() {
    const names = new Set(renderers.keys());
    for (const el of document.querySelectorAll("[data-slot]")) names.add(el.dataset.slot);
    return [...names].sort();
  }

  const isList = (el) => el.tagName === "UL" || el.tagName === "OL";
  const FIELDS = ["text", "html", "value", "state", "append", "attr"];

  function makeItem(parent, value, name) {
    const item = document.createElement(isList(parent) ? "li" : "div");
    renderDefault(item, value, name);
    return item;
  }

  // How a slot shows a value when the page didn't register its own renderer.
  function renderDefault(el, value, name) {
    if (value === null || value === undefined) { el.replaceChildren(); return; }
    if (Array.isArray(value)) { el.replaceChildren(...value.map((v) => makeItem(el, v, name))); return; }
    if (typeof value !== "object") {
      if (typeof value === "number" && "value" in el && el.tagName !== "LI") el.value = value;
      else el.textContent = String(value);
      return;
    }
    for (const k of Object.keys(value)) {
      if (!FIELDS.includes(k)) throw new Error(`slot "${name}": unknown field "${k}" (fields: ${FIELDS.join(", ")})`);
    }
    if ("html" in value) el.replaceChildren(fragment(value.html));
    if ("text" in value) el.textContent = String(value.text);
    if ("value" in value) el.value = value.value;
    if ("state" in value) el.dataset.state = String(value.state);
    if ("append" in value) {
      const items = Array.isArray(value.append) ? value.append : [value.append];
      for (const v of items) el.append(makeItem(el, v, name));
    }
    for (const [k, v] of Object.entries(value.attr || {})) {
      if (v === null) el.removeAttribute(k); else el.setAttribute(k, String(v));
    }
  }

  // Resolution order: renderer for the exact name, element with that data-slot,
  // then a renderer for the nearest dotted prefix ("plan" handles "plan.3").
  function applyUpdate(values) {
    for (const [name, value] of Object.entries(values || {})) {
      try {
        const exact = renderers.get(name);
        if (exact) { exact(value, { slot: name, key: "", el: slotElement(name) }); continue; }
        const el = slotElement(name);
        if (el) { renderDefault(el, value, name); continue; }
        const parts = name.split(".");
        let handled = false;
        for (let i = parts.length - 1; i > 0 && !handled; i--) {
          const prefix = parts.slice(0, i).join(".");
          const fn = renderers.get(prefix);
          if (fn) { fn(value, { slot: prefix, key: parts.slice(i).join("."), el: slotElement(prefix) }); handled = true; }
        }
        if (!handled) throw new Error(`unknown slot "${name}"; slots on this page: ${listSlots().join(", ") || "(none)"}`);
      } catch (e) {
        report(String(e.message || e));
      }
    }
  }

  // ---- automatic regions, fed by session events at no cost to the agent ----

  function toolLabel(ev) {
    const i = ev.input || {};
    const arg = i.description || i.command || i.file_path || i.pattern || i.url || i.query || "";
    return `${ev.name}${arg ? ": " + String(arg).slice(0, 120) : ""}`;
  }

  function autoFill(ev) {
    const isCanvasTool = ev.type === "tool" && ev.name.startsWith("mcp__canvas__");
    if (ev.type === "status") {
      for (const el of document.querySelectorAll('[data-auto="status"]')) {
        el.dataset.state = ev.busy ? "busy" : "idle";
        el.textContent = ev.busy ? "working" : "idle";
      }
    }
    if ((ev.type === "tool" && !isCanvasTool) || ev.type === "text") {
      const line = ev.type === "tool" ? toolLabel(ev) : ev.text.split("\n").find((l) => l.trim()) || "";
      for (const el of document.querySelectorAll('[data-auto="activity"]')) el.textContent = line;
    }
    if (ev.type === "tool" && !isCanvasTool) {
      for (const el of document.querySelectorAll('[data-auto="log"]')) {
        el.append(makeItem(el, toolLabel(ev), "log"));
        const max = Number(el.dataset.max) || 5;
        while (el.children.length > max) el.firstElementChild.remove();
      }
    }
  }

  function applyOp(op) {
    if (op.action === "update") { applyUpdate(op.values); return; }
    if (op.action === "script") {
      const s = document.createElement("script");
      s.textContent = op.js;
      document.body.appendChild(s);
      s.remove();
      return;
    }
    const els = document.querySelectorAll(op.selector);
    if (!els.length) throw new Error(`selector matched nothing: ${op.selector}`);
    for (const el of els) {
      switch (op.action) {
        case "inner": el.replaceChildren(fragment(op.html)); break;
        case "replace": el.replaceWith(fragment(op.html)); break;
        case "append": el.append(fragment(op.html)); break;
        case "prepend": el.prepend(fragment(op.html)); break;
        case "remove": el.remove(); break;
        case "text": el.textContent = op.text ?? ""; break;
        case "attr": el.setAttribute(op.name, op.value ?? ""); break;
        default: throw new Error(`unknown action: ${op.action}`);
      }
    }
  }

  function applyPatch(ops) {
    const errors = [];
    collecting = errors;
    try {
      for (const op of ops) {
        try { applyOp(op); } catch (e) { errors.push(String(e.message || e)); }
      }
    } finally {
      collecting = null;
    }
    return errors;
  }

  window.bridge = Object.freeze({
    version: boot.version,
    /** Send a message to the agent, as if typed by the user (prefixed "[canvas]"). */
    send(text) { post({ kind: "send", text: String(text) }); },
    /** Subscribe to session events; {replay: true} first delivers recent history. */
    onEvent(cb, opts) {
      if (opts && opts.replay) history.forEach((ev) => cb(ev));
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    /** Render `update` values for a slot (or "slot.*" keys) yourself: fn(value, {slot, key, el}). */
    onUpdate(slot, fn) {
      renderers.set(String(slot), fn);
      return () => renderers.delete(String(slot));
    },
  });

  window.addEventListener("message", (e) => {
    if (e.source !== parent) return;
    const msg = e.data || {};
    if (msg.kind === "patch") {
      const errors = applyPatch(msg.ops);
      post({ kind: "patched", index: msg.index, errors, slots: listSlots(), renderers: [...renderers.keys()] });
    } else if (msg.kind === "event" || msg.kind === "history") {
      const evs = msg.kind === "event" ? [msg.event] : msg.events;
      for (const ev of evs) {
        history.push(ev);
        if (history.length > 500) history.shift();
        try { autoFill(ev); } catch (err) { report(`data-auto: ${err.message}`); }
        for (const cb of listeners) {
          try { cb(ev); } catch (err) { report(`bridge.onEvent handler: ${err.message}`); }
        }
      }
    }
  });

  document.addEventListener("DOMContentLoaded", () => {
    // Replay patches made to this version before the frame loaded.
    for (const ops of boot.patches) startup.push(...applyPatch(ops));
    // Give the page's own scripts a moment to throw before declaring it loaded.
    setTimeout(() => {
      post({ kind: "ready", applied: boot.patches.length, errors: startup, slots: listSlots(), renderers: [...renderers.keys()] });
      startup = null;
    }, 300);
  });
})();
