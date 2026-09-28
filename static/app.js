"use strict";

const encoder = new TextEncoder();
const toast = document.querySelector("#toast");
let toastTimer;

function notify(message) {
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 3200);
}

function formatBytes(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(2)} MB`;
}

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); return; } catch (_) { /* Fall back to selection. */ }
  }
  const field = document.createElement("textarea");
  field.className = "clipboard-fallback";
  field.value = text;
  document.body.append(field);
  field.select();
  const copied = document.execCommand("copy");
  field.remove();
  if (!copied) throw new Error("clipboard unavailable");
}

const form = document.querySelector("#paste-form");
if (form) {
  const content = document.querySelector("#content");
  const syntax = document.querySelector("#syntax");
  const sizeLimit = Number(form.dataset.maxBytes);
  const error = document.querySelector("#editor-error");
  const submit = document.querySelector("#submit-button");
  let busy = false;
  let countFrame;

  function updateCount() {
    const size = encoder.encode(content.value).byteLength;
    const tooLarge = size > sizeLimit;
    document.querySelector("#byte-count").textContent = formatBytes(size);
    document.querySelector("#line-count").textContent = `${content.value.split("\n").length} 行`;
    document.querySelector("#editor-shell").classList.toggle("is-over-limit", tooLarge);
    error.textContent = tooLarge ? "文本超过 1 MB，请缩短后再创建 Paste。" : "";
    error.hidden = !tooLarge;
    content.setCustomValidity(tooLarge ? "文本不能超过 1 MB。" : "");
    content.setAttribute("aria-invalid", String(tooLarge));
    submit.disabled = busy || tooLarge;
    return !tooLarge;
  }

  content.addEventListener("input", () => {
    cancelAnimationFrame(countFrame);
    countFrame = requestAnimationFrame(updateCount);
  });
  syntax.addEventListener("change", () => {
    document.querySelector("#editor-format").textContent = syntax.value === "text" ? "Plain text" : syntax.selectedOptions[0].textContent;
  });
  form.addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      if (!busy && updateCount()) form.requestSubmit();
    }
  });
  form.addEventListener("submit", event => {
    if (busy || !updateCount()) {
      event.preventDefault();
      return;
    }
    if (!content.value.trim()) {
      event.preventDefault();
      error.textContent = "请先输入要分享的文本。";
      error.hidden = false;
      content.focus();
      return;
    }
    // Native submission keeps the content available in browser history if it fails.
    busy = true;
    submit.disabled = true;
    submit.textContent = "正在创建…";
  });
  window.addEventListener("pageshow", () => {
    busy = false;
    submit.textContent = "创建 Paste →";
    updateCount();
  });
  updateCount();
  syntax.dispatchEvent(new Event("change"));
}

document.querySelectorAll("time[datetime]").forEach(element => {
  const date = new Date(element.dateTime);
  element.textContent = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date);
  element.title = date.toLocaleString("zh-CN", { timeZoneName: "short" });
});

document.querySelector("#share-url")?.addEventListener("click", event => event.target.select());
document.querySelector("#copy-link")?.addEventListener("click", async () => {
  try { await copyText(document.querySelector("#share-url").value); notify("分享链接已复制"); }
  catch (_) { document.querySelector("#share-url").select(); notify("请手动复制已选中的链接"); }
});
document.querySelector("#copy-content")?.addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const response = await fetch(button.dataset.rawUrl, { cache: "no-store" });
    if (!response.ok) throw new Error("unavailable");
    await copyText(await response.text());
    notify("文本内容已复制");
  } catch (_) { notify("未能复制，请通过「原文」查看并复制"); }
  finally { button.disabled = false; }
});
document.querySelector("#wrap-toggle")?.addEventListener("click", event => {
  const wrapped = document.querySelector("#code-scroll").classList.toggle("is-wrapped");
  event.currentTarget.setAttribute("aria-pressed", String(wrapped));
});

const expiry = document.querySelector("[data-expires-at]");
if (expiry) {
  const expiresAt = Number(expiry.dataset.expiresAt) * 1000;
  let expiryTimer;
  function updateExpiry() {
    const remaining = expiresAt - Date.now();
    const label = document.querySelector("#expiry-countdown");
    if (remaining <= 0) {
      clearInterval(expiryTimer);
      label.textContent = "已到期";
      document.querySelector("#paste-content").textContent = "这条 Paste 已到期。";
      const numbers = document.querySelector(".line-numbers");
      if (numbers) numbers.textContent = "";
      document.querySelector("#copy-content").disabled = true;
      return;
    }
    const minutes = Math.ceil(remaining / 60000);
    label.textContent = minutes > 1440 ? `约 ${Math.ceil(minutes / 1440)} 天后到期` : minutes > 60 ? `约 ${Math.ceil(minutes / 60)} 小时后到期` : `${minutes} 分钟后到期`;
  }
  expiryTimer = setInterval(updateExpiry, 15000);
  updateExpiry();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) updateExpiry(); });
}
