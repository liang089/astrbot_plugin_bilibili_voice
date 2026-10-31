const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

const qrImage = $("qr-image");
const qrPlaceholder = $("qr-placeholder");
const statusBadge = $("status-badge");
const loginMsg = $("login-msg");
const searchResults = $("search-results");
const searchMsg = $("search-msg");
const downloadSection = $("download-section");
const progressFill = $("progress-fill");
const downloadMsg = $("download-msg");

let searchData = [];

// Wait for the AstrBot plugin view bridge.
const context = await bridge.ready();
console.log("Bridge ready", context);

checkStatus();

// ---- Login ----
$("btn-login").addEventListener("click", async () => {
  loginMsg.textContent = "正在获取二维码...";
  try {
    const data = await bridge.apiGet("qrcode");
    showQr(data);
    loginMsg.textContent = "请用哔哩哔哩 APP 扫码登录";
    loginMsg.className = "msg";
    pollLogin();
  } catch (e) {
    loginMsg.textContent = "获取二维码失败: " + (e.message || e);
    loginMsg.className = "msg error";
  }
});

$("btn-refresh").addEventListener("click", async () => {
  try {
    const data = await bridge.apiGet("qrcode/refresh");
    showQr(data);
    loginMsg.textContent = "二维码已刷新";
    loginMsg.className = "msg";
    statusBadge.className = "badge badge-refresh";
    statusBadge.textContent = "等待扫码";
  } catch (e) {
    loginMsg.textContent = "刷新失败: " + (e.message || e);
    loginMsg.className = "msg error";
  }
});

$("btn-status").addEventListener("click", checkStatus);

async function checkStatus() {
  try {
    const data = await bridge.apiGet("status");
    if (data.logged_in) {
      statusBadge.className = "badge badge-online";
      statusBadge.textContent = data.nickname || "已登录";
      loginMsg.textContent = "已登录: " + (data.nickname || "");
      loginMsg.className = "msg success";
    } else {
      statusBadge.className = "badge badge-offline";
      statusBadge.textContent = "未登录";
      loginMsg.textContent = "未登录，请点击「获取二维码」";
      loginMsg.className = "msg";
    }
  } catch (e) {
    statusBadge.className = "badge badge-offline";
    statusBadge.textContent = "未登录";
  }
}

function showQr(data) {
  const url =
    data.qrcode_url ||
    (data.image ? "" : "");
  if (data.image) {
    qrImage.src = data.image;
  } else if (url) {
    qrImage.src =
      "https://api.qrserver.com/v1/create-qr-code/?size=280x280&data=" +
      encodeURIComponent(url);
  } else {
    qrPlaceholder.textContent = "二维码获取失败，请重试";
    return;
  }
  qrImage.style.display = "block";
  qrPlaceholder.style.display = "none";
}

async function pollLogin() {
  for (let i = 0; i < 30; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    try {
      const data = await bridge.apiGet("status");
      if (data.logged_in) {
        statusBadge.className = "badge badge-online";
        statusBadge.textContent = data.nickname || "已登录";
        loginMsg.textContent = "登录成功: " + (data.nickname || "");
        loginMsg.className = "msg success";
        return;
      }
    } catch (e) {
      // Keep polling; temporary network errors are common.
    }
  }
  loginMsg.textContent = "二维码已过期，请点击「刷新二维码」";
  loginMsg.className = "msg error";
}

// ---- Search ----
$("btn-search").addEventListener("click", doSearch);
$("search-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") doSearch();
});

async function doSearch() {
  const keyword = $("search-input").value.trim();
  if (!keyword) return;
  $("btn-search").disabled = true;
  searchResults.innerHTML = "<p class='hint'>搜索中...</p>";
  searchMsg.textContent = "";
  try {
    const data = await bridge.apiGet("search", { keyword });
    searchData = data.results || [];
    if (searchData.length === 0) {
      searchResults.innerHTML = "<p class='hint'>没有找到结果</p>";
      return;
    }
    searchResults.innerHTML = searchData
      .map((item, i) => {
        const pageFlag = item.page ? ` P${item.page}` : "";
        const cover = item.cover
          ? `<img class="result-cover" src="${escapeAttr(item.cover)}" alt="" loading="lazy" />`
          : `<div class="result-cover placeholder">${item.bvid ? "B站" : "视频"}</div>`;
        return `<div class="result-item">
          ${cover}
          <div class="result-info">
            <div class="result-title">${escapeHtml(item.title)}</div>
            <div class="result-meta">${escapeHtml(item.author)} / ${escapeHtml(item.duration || "?")}${pageFlag}</div>
          </div>
          <button class="result-dl-btn" data-index="${i}">下载音频</button>
        </div>`;
      })
      .join("");
    searchMsg.textContent = `找到 ${searchData.length} 个结果`;
    searchMsg.className = "msg success";
    document.querySelectorAll(".result-dl-btn").forEach((btn) => {
      btn.addEventListener("click", () => doDownload(parseInt(btn.dataset.index, 10), btn));
    });
  } catch (e) {
    searchResults.innerHTML = "";
    searchMsg.textContent = "搜索失败: " + (e.message || e);
    searchMsg.className = "msg error";
  } finally {
    $("btn-search").disabled = false;
  }
}

async function doDownload(index, button) {
  const item = searchData[index];
  if (!item) return;
  if (button) button.disabled = true;
  downloadSection.style.display = "block";
  progressFill.style.width = "10%";
  const pMsg = item.page ? ` (P${item.page})` : "";
  downloadMsg.textContent = "正在下载并转码 " + item.title + pMsg + "...";
  downloadMsg.className = "msg";
  try {
    const params = { url: item.url };
    if (item.page) params.page = item.page;
    const result = await bridge.apiGet("download", params);
    progressFill.style.width = "90%";
    downloadMsg.textContent = "下载完成，正在保存文件...";
    if (result.token && typeof bridge.download === "function") {
      await bridge.download(
        "download/file",
        { token: result.token },
        result.filename || "bilibili-audio"
      );
      downloadMsg.textContent =
        "已保存到本地: " + (result.filename || "") +
        (result.size ? `（${formatSize(result.size)}）` : "");
    } else {
      downloadMsg.textContent =
        "音频已生成：" + (result.file || result.filename || "") +
        "（当前 AstrBot 版本不支持浏览器下载，请升级到 4.26+）";
    }
    progressFill.style.width = "100%";
    downloadMsg.className = "msg success";
  } catch (e) {
    downloadMsg.textContent = "下载失败: " + (e.message || e);
    downloadMsg.className = "msg error";
  } finally {
    if (button) button.disabled = false;
  }
}

function formatSize(bytes) {
  if (!Number.isFinite(bytes)) return "";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = String(text ?? "");
  return div.innerHTML;
}

function escapeAttr(text) {
  return escapeHtml(text).replaceAll('"', "&quot;");
}
