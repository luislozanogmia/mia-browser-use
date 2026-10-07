// The page shown once after install. It has one job: hand over the installer, then
// show "ready" the moment the helper answers. The background keeps asking for the
// helper every few seconds while it's missing, so nothing here starts anything.
const $ = id => document.getElementById(id);

function show(info) {
  if (!info) return;
  $("download").href = info.installer_url || "#";
  const ready = Boolean(info.connected);
  const paired = ready || Boolean(info.paired) || info.helper === "ok";
  $("setup").hidden = paired;
  $("status").classList.toggle("ready", ready);
  if (ready) {
    $("statusTitle").textContent = "Mia is ready";
    $("statusText").textContent = "Click the Mia icon in Chrome's toolbar to open her, then Sign in to Claude the first time. You can close this tab.";
  } else if (paired) {
    $("statusTitle").textContent = "Starting Mia…";
    $("statusText").textContent = "The helper is installed; Mia is starting on this computer.";
  } else if (info.helper === "outdated") {
    $("statusTitle").textContent = "An older Mia helper is installed";
    $("statusText").textContent = "Run the current installer above; it replaces the old one and keeps your settings.";
  } else {
    $("statusTitle").textContent = "Waiting for the installer…";
    $("statusText").textContent = "This page checks every few seconds. Nothing to click.";
  }
}

function poll() {
  chrome.runtime.sendMessage({ type: "get-status" }, info => {
    if (!chrome.runtime.lastError) show(info);
  });
}
poll();
setInterval(poll, 3000);
