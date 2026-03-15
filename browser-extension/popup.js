const statusEl = document.getElementById("status");
const formEl = document.getElementById("save-form");
const textEl = document.getElementById("text");
const tagsEl = document.getElementById("tags");
const noteEl = document.getElementById("note");
const saveButtonEl = document.getElementById("save-button");
const pageTitleEl = document.getElementById("page-title");
const pageUrlEl = document.getElementById("page-url");

const extensionApi = globalThis.browser ?? globalThis.chrome;
let currentPage = null;

function setStatus(message, kind = "muted") {
  statusEl.textContent = message;
  statusEl.className = `status status--${kind}`;
}

async function getSettings() {
  if (globalThis.browser?.storage?.sync) {
    return globalThis.browser.storage.sync.get({
      baseUrl: "",
      apiKey: "",
      defaultTags: ""
    });
  }
  return new Promise((resolve, reject) => {
    globalThis.chrome.storage.sync.get(
      { baseUrl: "", apiKey: "", defaultTags: "" },
      (result) => {
        const lastError = globalThis.chrome.runtime?.lastError;
        if (lastError) {
          reject(new Error(lastError.message));
          return;
        }
        resolve(result);
      }
    );
  });
}

async function getActiveTab() {
  const [tab] = await extensionApi.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function captureSelection(tabId) {
  if (!extensionApi.scripting?.executeScript) {
    throw new Error("This browser does not support popup selection capture here.");
  }

  const executionResults = await extensionApi.scripting.executeScript({
    target: { tabId },
    func: () => {
      const selection = window.getSelection();
      const text = selection ? selection.toString().trim() : "";
      return {
        text,
        title: document.title || "",
        url: window.location.href || ""
      };
    }
  });
  const executionResult = Array.isArray(executionResults) ? executionResults[0] : null;
  if (!executionResult || typeof executionResult.result !== "object" || !executionResult.result) {
    throw new Error("Unable to read selection on this page. Try a regular webpage tab.");
  }
  return executionResult.result;
}

async function initialize() {
  try {
    const settings = await getSettings();
    const tab = await getActiveTab();
    if (!tab?.id) {
      setStatus("No active tab available.", "error");
      return;
    }

    const page = await captureSelection(tab.id);
    currentPage = page;
    tagsEl.value = settings.defaultTags || "";
    pageTitleEl.textContent = page.title || "Untitled page";
    pageUrlEl.textContent = page.url || "";

    if (!settings.baseUrl || !settings.apiKey) {
      setStatus("Set your PHM base URL and API key in Settings first.", "error");
      return;
    }

    if (!page.text) {
      setStatus("Select text on the page, then open the extension.", "error");
      return;
    }

    textEl.value = page.text;
    formEl.hidden = false;
    setStatus("Ready to save.", "muted");
  } catch (error) {
    setStatus(error.message || "Unable to read the current page.", "error");
  }
}

formEl.addEventListener("submit", async (event) => {
  event.preventDefault();
  const settings = await getSettings();
  if (!settings.baseUrl || !settings.apiKey || !currentPage?.text) {
    setStatus("Missing configuration or selected text.", "error");
    return;
  }

  const reminderValue = new FormData(formEl).get("reminder");
  const formData = new FormData();
  formData.set("text", currentPage.text);
  formData.set("source_url", currentPage.url || "");
  formData.set("source_title", currentPage.title || "");
  if (tagsEl.value.trim()) {
    formData.set("tags", tagsEl.value.trim());
  }
  if (noteEl.value.trim()) {
    formData.set("note", noteEl.value.trim());
  }
  if (reminderValue) {
    formData.set("reminder_preset", reminderValue);
  }

  saveButtonEl.disabled = true;
  setStatus("Saving…", "muted");

  try {
    const response = await fetch(`${settings.baseUrl.replace(/\/$/, "")}/api/highlights`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${settings.apiKey}`
      },
      body: formData
    });

    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.detail || `Save failed (${response.status})`);
    }

    setStatus("Highlight saved.", "success");
    saveButtonEl.textContent = "Saved";
  } catch (error) {
    setStatus(error.message || "Save failed.", "error");
    saveButtonEl.disabled = false;
  }
});

initialize();
