// SONU's browser extension (D70, D71, D72): answers SONU — on this computer only, through the browser's
// native messaging — about the tabs (list them, switch to one, close some), and acts on one page only when
// SONU asks: its text, its controls' names, a click, the user's words typed into a field, scrolling.
// Nothing else, nowhere else.
"use strict";

const api = globalThis.browser ?? globalThis.chrome;
const HOST = "dev.sonu.browser";
const RETRY_MS = 10000;
let port = null;

function browserName() {
  const agent = navigator.userAgent;
  if (/Firefox\//.test(agent)) return agent.includes("Zen") ? "zen" : "firefox";
  return /Edg\//.test(agent) ? "edge" : "chromium";
}

function tabInfo(tab) {
  return {
    id: tab.id,
    window: tab.windowId,
    index: tab.index,
    active: tab.active,
    title: tab.title ?? "",
    url: tab.url ?? "",
    audible: Boolean(tab.audible),
  };
}

// Runs in the page (the browser copies the function there): the text a person sees — the article or
// main part when the page has one, else the whole body — and what's selected.
function pageText(limit) {
  const clean = (text) => String(text ?? "").replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
  const main = document.querySelector("article, main, [role=main]");
  const mainText = clean(main?.innerText);
  const text = mainText.length >= 500 ? mainText : clean(document.body?.innerText);
  return {
    title: document.title,
    url: location.href,
    text: text.slice(0, limit),
    length: text.length,
    selection: clean(globalThis.getSelection?.()?.toString()).slice(0, limit),
  };
}

// Runs in the page (copied there on its own, so everything it needs is inside). Controls get a
// data-sonu-click / data-sonu-fill number in document order, so the same page lists the same numbers
// again; SONU acts on one by its number.
function pageAction(op, args) {
  const text = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
  const visible = (el) => {
    const box = el.getBoundingClientRect();
    if (box.width < 1 || box.height < 1) return false;
    const style = getComputedStyle(el);
    return style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) > 0;
  };
  const inView = (el) => {
    const box = el.getBoundingClientRect();
    return box.bottom > 0 && box.top < innerHeight && box.right > 0 && box.left < innerWidth;
  };
  const labelOf = (el) => {
    const ids = el.getAttribute("aria-labelledby");
    const byIds = ids ? text(ids.split(/\s+/).map((id) => document.getElementById(id)?.innerText).join(" ")) : "";
    return byIds || text(el.getAttribute("aria-label")) || text([...(el.labels ?? [])].map((l) => l.innerText).join(" "));
  };
  const CLICK = "a[href], button, input[type=button], input[type=submit], input[type=reset], input[type=checkbox], " +
    "input[type=radio], [role=button], [role=link], [role=tab], [role=menuitem], [role=checkbox], [role=switch], " +
    "[role=option], summary";
  const FILL = "input:not([type]), input[type=text], input[type=search], input[type=email], input[type=url], " +
    "input[type=tel], input[type=number], input[type=password], textarea, [contenteditable=''], " +
    "[contenteditable=true], [role=textbox], [role=searchbox], [role=combobox]";
  const secret = (el) => el.type === "password" || /^cc-|one-time-code|current-password|new-password/.test(el.autocomplete ?? "");
  const value = (el) => (el.isContentEditable ? el.innerText : el.value ?? "");
  const find = (kind, ref) => document.querySelector(`[data-sonu-${kind}="${Number(ref)}"]`);
  const kindOf = (el) => {
    const role = el.getAttribute("role");
    if (el.matches("input[type=checkbox], input[type=radio]") || ["checkbox", "switch"].includes(role)) return "checkbox";
    if (el.matches("a[href]") && !/^javascript:/i.test(el.getAttribute("href"))) return "link";
    return "button";
  };
  const state = (el) => el ? {
    checked: el.checked ?? el.getAttribute("aria-checked"),
    expanded: el.getAttribute("aria-expanded"),
    pressed: el.getAttribute("aria-pressed"),
    selected: el.getAttribute("aria-selected"),
  } : null;
  const page = () => ({ url: location.href, title: document.title, size: document.body?.innerText.length ?? 0 });

  if (op === "controls") {
    const kind = args.kind === "fill" ? "fill" : "click";
    const controls = [];
    let ref = 0;
    for (const el of document.querySelectorAll(kind === "fill" ? FILL : CLICK)) {
      if (!visible(el) || el.disabled || el.getAttribute("aria-disabled") === "true" || (kind === "fill" && el.readOnly)) continue;
      ref += 1;
      el.setAttribute(`data-sonu-${kind}`, String(ref));
      const name = kind === "fill"
        ? labelOf(el) || text(el.placeholder) || text(el.title) || text(el.name)
          || (el.matches("input[type=search], [role=searchbox]") ? "search" : "")
        : labelOf(el) || text(el.innerText) || text(el.matches("input") ? el.value : "") || text(el.title)
          || text(el.querySelector("img[alt]")?.alt);
      let host = "";
      try { host = el.matches("a[href]") ? new URL(el.href).host : ""; } catch { host = ""; }
      controls.push({
        ref, name: name.slice(0, 120), kind: kind === "fill" ? "field" : kindOf(el),
        secret: kind === "fill" && secret(el), host, in_view: inView(el),
      });
      if (controls.length >= 400) break;
    }
    return { controls, ...page() };
  }
  if (op === "click") {
    const el = find("click", args.ref);
    if (!el) return { found: false };
    const before = { ...page(), control: state(el) };
    el.scrollIntoView({ block: "center", behavior: "instant" });
    el.focus?.({ preventScroll: true });
    el.click();
    return { found: true, before };
  }
  if (op === "state") {
    return { ...page(), control: state(args.ref ? find("click", args.ref) : null) };
  }
  if (op === "fill") {
    const el = find("fill", args.ref);
    if (!el) return { found: false };
    if (secret(el)) return { found: true, refused: "a password or card field" };
    el.scrollIntoView({ block: "center", behavior: "instant" });
    el.focus();
    if (el.isContentEditable) {
      const range = document.createRange();
      range.selectNodeContents(el);
      getSelection().removeAllRanges();
      getSelection().addRange(range);
    } else {
      el.select?.();
    }
    // Typed like a person (sites that watch keystrokes see it), else set like a form would be.
    const typed = document.execCommand("insertText", false, args.text);
    if (!typed || text(value(el)) !== text(args.text)) {
      if (el.isContentEditable) {
        el.textContent = args.text;
      } else {
        const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value")?.set;
        if (setter) setter.call(el, args.text); else el.value = args.text;
      }
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }
    const filled = value(el);
    let submitted = false;
    if (args.submit) {
      const before = page();
      const key = (type) => el.dispatchEvent(new KeyboardEvent(type, {
        key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true,
      }));
      const notHandled = key("keydown");
      key("keypress");
      key("keyup");
      // A one-line field sends its form on Enter; in a text box Enter is a new line (the page's own keys decide).
      if (notHandled && el.form && el.matches("input")) {
        if (el.form.requestSubmit) el.form.requestSubmit(); else el.form.submit();
      }
      submitted = true;
      return { found: true, value: filled, submitted, before };
    }
    return { found: true, value: filled, submitted };
  }
  if (op === "scroll") {
    // The page itself, or — when only a part of it scrolls (mail, chat apps) — its biggest scrolling part.
    let box = document.scrollingElement ?? document.documentElement;
    if (box.scrollHeight <= box.clientHeight + 4) {
      let best = 0;
      for (const el of document.querySelectorAll("body *")) {
        const overflow = getComputedStyle(el).overflowY;
        if ((overflow === "auto" || overflow === "scroll") && el.scrollHeight > el.clientHeight + 40 && visible(el)) {
          const area = el.clientWidth * el.clientHeight;
          if (area > best) { best = area; box = el; }
        }
      }
    }
    const before = box.scrollTop;
    const step = box.clientHeight * 0.8;
    const top = { down: before + step, up: before - step, top: 0, bottom: box.scrollHeight }[args.how] ?? before;
    box.scrollTo({ top, behavior: "instant" });
    return { before, after: box.scrollTop, end: Math.max(0, box.scrollHeight - box.clientHeight) };
  }
  throw new Error(`unknown page action ${op}`);
}

async function inPage(tabId, func, args) {
  const allowed = await api.permissions.contains({ origins: ["<all_urls>"] });
  if (!allowed) {
    const where = browserName() === "chromium" || browserName() === "edge"
      ? "chrome://extensions → SONU → Details: Site access “On all sites”"
      : "about:addons → SONU → Permissions: “Access your data for all websites”";
    throw new Error(`SONU's extension isn't allowed to read pages — turn it on in ${where}`);
  }
  const [found] = await api.scripting.executeScript({ target: { tabId }, func, args });
  if (!found || found.result == null) throw new Error("the page didn't answer");
  return found.result;
}

async function answer(message) {
  switch (message.op) {
    case "tabs": {
      const tabs = await api.tabs.query({});
      const focused = await api.windows.getLastFocused();
      return { tabs: tabs.map(tabInfo), focused_window: focused?.id ?? null };
    }
    case "activate": {
      const tab = await api.tabs.update(message.tab, { active: true });
      await api.windows.update(tab.windowId, { focused: true });
      return { tab: tabInfo(await api.tabs.get(message.tab)) };
    }
    case "close": {
      await api.tabs.remove(message.tabs);
      return { closed: message.tabs.length };
    }
    case "page": {
      // Only when SONU asks for one tab: its visible text, for the local model on this computer.
      const limit = Math.max(1000, Math.min(Number(message.limit) || 20000, 100000));
      return inPage(message.tab, pageText, [limit]);
    }
    case "controls":
    case "click":
    case "state":
    case "fill":
    case "scroll":
      return inPage(message.tab, pageAction, [message.op, message.args ?? {}]);
    default:
      throw new Error(`unknown op ${message.op}`);
  }
}

function connect() {
  port = api.runtime.connectNative(HOST);
  port.onMessage.addListener(async (message) => {
    let reply;
    try {
      reply = { id: message.id, ok: true, result: await answer(message) };
    } catch (error) {
      reply = { id: message.id, ok: false, error: String(error?.message ?? error) };
    }
    port?.postMessage(reply);
  });
  port.onDisconnect.addListener(() => {
    port = null;
    setTimeout(connect, RETRY_MS); // SONU's helper isn't installed yet, or it stopped
  });
  port.postMessage({ hello: { browser: browserName(), version: api.runtime.getManifest().version } });
}

connect();
