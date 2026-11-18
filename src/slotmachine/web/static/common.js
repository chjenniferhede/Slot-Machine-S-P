// Shared by both pages: the API client, safe element building, and language.
//
// Safety rule for every page: text from the server or the user is only ever
// put on the page with textContent (through el() below), never as HTML.
// A source scan in the test suite refuses innerHTML, outerHTML,
// insertAdjacentHTML, document.write, eval and new Function in these files.

import { STRINGS } from "/strings.js";

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

export const api = {
  csrf: null,

  async me() {
    const reply = await fetch("/api/me", { credentials: "same-origin", cache: "no-store" });
    const body = await reply.json();
    this.csrf = typeof body.csrf === "string" ? body.csrf : null;
    return body;
  },

  async post(path, body = {}) {
    const headers = { "Content-Type": "application/json", "X-Casino": "1" };
    if (this.csrf) headers["X-CSRF"] = this.csrf;
    const reply = await fetch(path, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      credentials: "same-origin",
      cache: "no-store",
    });
    let data = {};
    try {
      data = await reply.json();
    } catch {
      data = {};
    }
    if (!reply.ok) throw new ApiError(data.error || t("errorGeneric"), reply.status);
    if (data && typeof data.csrf === "string") this.csrf = data.csrf;
    return data;
  },
};

// ---------------------------------------------------------------------------
// Building elements, text only
// ---------------------------------------------------------------------------

const SAFE_ATTRIBUTES = new Set([
  "type", "placeholder", "value", "name", "autocomplete", "inputmode", "maxlength",
  "min", "max", "step", "for", "id", "role", "aria-label", "aria-live", "title",
  "src", "alt", "href", "rows", "spellcheck", "disabled", "checked", "selected", "required",
]);

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (name === "class") node.className = value;
    else if (name === "text") node.textContent = String(value);
    else if (name.startsWith("on") && typeof value === "function") node.addEventListener(name.slice(2), value);
    else if (name === "dataset") Object.assign(node.dataset, value);
    else if (SAFE_ATTRIBUTES.has(name)) {
      if (name === "href" && !String(value).startsWith("#") && !String(value).startsWith("/")) continue;
      node.setAttribute(name, value === true ? "" : String(value));
    }
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function clear(node, ...children) {
  node.replaceChildren();
  append(node, children);
  return node;
}

// ---------------------------------------------------------------------------
// Language
// ---------------------------------------------------------------------------

const LANG_KEY = "casino-lang";
let lang = "zh";
try {
  const saved = localStorage.getItem(LANG_KEY);
  if (saved === "zh" || saved === "en") lang = saved;
} catch {
  // Storage can be blocked; the default language is fine.
}
const listeners = [];

export function getLang() {
  return lang;
}

export function toggleLang() {
  lang = lang === "zh" ? "en" : "zh";
  try {
    localStorage.setItem(LANG_KEY, lang);
  } catch {
    // Not remembered this time; nothing else depends on it.
  }
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  for (const listener of listeners) listener(lang);
}

export function onLangChange(listener) {
  listeners.push(listener);
}

export function t(key, vars = {}) {
  const table = STRINGS[lang] || STRINGS.zh;
  let text = table[key] ?? STRINGS.zh[key] ?? key;
  for (const [name, value] of Object.entries(vars)) text = text.split(`{${name}}`).join(String(value));
  return text;
}

export function pick(table) {
  return table[lang] || table.zh;
}

document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

export function pct(value, digits = 2) {
  return typeof value === "number" ? `${(value * 100).toFixed(digits)}%` : "—";
}

export function num(value, digits = 0) {
  if (typeof value !== "number") return "—";
  return value.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

export function roleName(role) {
  return t({ player: "rolePlayer", technician: "roleTechnician", commissioner: "roleCommissioner" }[role] || "rolePlayer");
}

export function shortTime(stamp) {
  return typeof stamp === "string" ? stamp.replace("T", " ").replace("Z", "") : "—";
}

// A form's fields by name, as text. Amounts stay text: the server parses them.
export function formValues(form) {
  const values = {};
  for (const field of form.elements) {
    if (field.name) values[field.name] = field.value;
  }
  return values;
}
