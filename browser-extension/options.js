const formEl = document.getElementById("options-form");
const statusEl = document.getElementById("status");
const baseUrlEl = document.getElementById("base-url");
const apiKeyEl = document.getElementById("api-key");
const defaultTagsEl = document.getElementById("default-tags");

const extensionApi = globalThis.browser ?? globalThis.chrome;

function setStatus(message, kind = "muted") {
  statusEl.textContent = message;
  statusEl.className = `status status--${kind}`;
}

async function loadSettings() {
  const settings = globalThis.browser?.storage?.sync
    ? await globalThis.browser.storage.sync.get({
        baseUrl: "",
        apiKey: "",
        defaultTags: ""
      })
    : await new Promise((resolve, reject) => {
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
  baseUrlEl.value = settings.baseUrl || "";
  apiKeyEl.value = settings.apiKey || "";
  defaultTagsEl.value = settings.defaultTags || "";
}

formEl.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = {
    baseUrl: baseUrlEl.value.trim().replace(/\/$/, ""),
    apiKey: apiKeyEl.value.trim(),
    defaultTags: defaultTagsEl.value.trim()
  };
  if (globalThis.browser?.storage?.sync) {
    await globalThis.browser.storage.sync.set(payload);
  } else {
    await new Promise((resolve, reject) => {
      globalThis.chrome.storage.sync.set(payload, () => {
        const lastError = globalThis.chrome.runtime?.lastError;
        if (lastError) {
          reject(new Error(lastError.message));
          return;
        }
        resolve();
      });
    });
  }
  setStatus("Settings saved.", "success");
});

loadSettings();
