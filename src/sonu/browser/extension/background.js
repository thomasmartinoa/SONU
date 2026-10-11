// SONU's browser extension (D70, D71): answers SONU — on this computer only, through the browser's native
// messaging — about the tabs: list them, switch to one, close some, and a page's text when SONU asks.
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
      const allowed = await api.permissions.contains({ origins: ["<all_urls>"] });
      if (!allowed) {
        const where = browserName() === "chromium" || browserName() === "edge"
          ? "chrome://extensions → SONU → Details: Site access “On all sites”"
          : "about:addons → SONU → Permissions: “Access your data for all websites”";
        throw new Error(`SONU's extension isn't allowed to read pages — turn it on in ${where}`);
      }
      const limit = Math.max(1000, Math.min(Number(message.limit) || 20000, 100000));
      const [found] = await api.scripting.executeScript({
        target: { tabId: message.tab },
        func: pageText,
        args: [limit],
      });
      if (!found || found.result == null) throw new Error("the browser didn't return the page's text");
      return found.result;
    }
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
