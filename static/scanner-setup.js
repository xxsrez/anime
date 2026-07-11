const copyButton = document.getElementById("copy-extensions-url");
const setupState = document.getElementById("setup-state");

copyButton?.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText("chrome://extensions");
    setupState.textContent = "Адрес скопирован.";
  } catch (error) {
    setupState.textContent = "Скопируйте chrome://extensions вручную.";
  }
});
