const bridge = window.AstrBotPluginPage;
const $ = id => document.getElementById(id);

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

// Wait for bridge
const context = await bridge.ready();
console.log("Bridge ready", context);

// Check login status on load
checkStatus();

// ---- Login ----
$("btn-login").addEventListener("click", async () => {
  try {
    const data = await bridge.apiGet("qrcode");
    showQr(data.qrcode_url);
    loginMsg.textContent = "请用哔哩哔哩APP扫码登录";
    pollLogin();
  } catch (e) {
    loginMsg.textContent = "获取二维码失败: " + e.message;
  }
});

$("btn-refresh").addEventListener("click", async () => {
  try {
    const data = await bridge.apiGet("qrcode/refresh");
    showQr(data.qrcode_url);
    loginMsg.textContent = "二维码已刷新";
    statusBadge.className = "badge badge-refresh";
    statusBadge.textContent = "等待扫码";
  } catch (e) {
    loginMsg.textContent = "刷新失败: " + e.message;
  }
});

$("btn-status").addEventListener("click", checkStatus);

async function checkStatus() {
  try {
    const data = await bridge.apiGet("status");
    if (data.logged_in) {
      statusBadge.className = "badge badge-online";
      statusBadge.textContent = data.username || "已登录";
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

function showQr(url) {
  qrImage.src = "https://api.qrserver.com/v1/create-qr-code/?size=280x280&data=" + encodeURIComponent(url);
  qrImage.style.display = "block";
  qrPlaceholder.style.display = "none";
}

async function pollLogin() {
  for (let i = 0; i < 30; i++) {
    await new Promise(r => setTimeout(r, 2000));
    try {
      const data = await bridge.apiGet("status");
      if (data.logged_in) {
        statusBadge.className = "badge badge-online";
        statusBadge.textContent = data.nickname || "已登录";
        loginMsg.textContent = "登录成功: " + (data.nickname || "");
        loginMsg.className = "msg success";
        return;
      }
    } catch (e) {}
  }
}

// ---- Search ----
$("btn-search").addEventListener("click", doSearch);
$("search-input").addEventListener("keydown", e => {
  if (e.key === "Enter") doSearch();
});

async function doSearch() {
  const keyword = $("search-input").value.trim();
  if (!keyword) return;
  searchResults.innerHTML = "<p style='color:#9ca3af'>搜索中...</p>";
  try {
    const data = await bridge.apiGet("search", { keyword });
    searchData = data.results || [];
    if (searchData.length === 0) {
      searchResults.innerHTML = "<p>没有找到结果</p>";
      return;
    }
    searchResults.innerHTML = searchData.map((item, i) => {
      const pageFlag = item.page ? ` P${item.page}` : "";
      return `<div class="result-item">
        <div class="result-info">
          <div class="result-title">${escapeHtml(item.title)}</div>
          <div class="result-meta">${escapeHtml(item.author)} / ${item.duration || "?"}${pageFlag}</div>
        </div>
        <button class="result-dl-btn" data-index="${i}">下载MP3</button>
      </div>`;
    }).join("");
    searchMsg.textContent = `找到 ${searchData.length} 个结果`;
    searchMsg.className = "msg success";
    // Bind download buttons
    document.querySelectorAll(".result-dl-btn").forEach(btn => {
      btn.addEventListener("click", () => doDownload(parseInt(btn.dataset.index)));
    });
  } catch (e) {
    searchResults.innerHTML = "";
    searchMsg.textContent = "搜索失败: " + e.message;
    searchMsg.className = "msg error";
  }
}

async function doDownload(index) {
  const item = searchData[index];
  if (!item) return;
  downloadSection.style.display = "block";
  progressFill.style.width = "0%";
  const pMsg = item.page ? ` (P${item.page})` : "";
  downloadMsg.textContent = "正在下载 " + item.title + pMsg + "...";
  downloadMsg.className = "msg";
  try {
    const params = { url: item.url, title: item.title };
    if (item.page) params.page = item.page;
    const result = await bridge.apiGet("download", params);
    progressFill.style.width = "100%";
    downloadMsg.textContent = "下载完成: " + (result.file || "");
    downloadMsg.className = "msg success";
  } catch (e) {
    downloadMsg.textContent = "下载失败: " + e.message;
    downloadMsg.className = "msg error";
  }
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}
