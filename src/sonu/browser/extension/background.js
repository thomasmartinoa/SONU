// SONU's browser extension (D70): answers SONU — on this computer only, through the browser's native
// messaging — about the tabs: list them, switch to one, close some. Nothing else, nowhere else.
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
