/* Cortech Chat 小红书助手
 * 只在用户点击后读取当前浏览器里已有的小红书 Cookie，确认后再发送。
 * 不注册后台监听，不把 Cookie 写入界面或 chrome.storage。
 */
const ALLOWED = ["a1", "web_session", "webId", "gid", "websectiga", "sec_poison_id",
  "xsecappid", "abRequestId", "webBuild", "acw_tc", "customerClientId", "unread"];
const XHS_DOMAINS = ["xiaohongshu.com", "xiaohongshu.cn", "xiaohongshu.hk"];

const originInput = document.getElementById("origin");
const previewBox = document.getElementById("preview-box");
const confirmBox = document.getElementById("confirm");
const sendBtn = document.getElementById("send");
const statusEl = document.getElementById("status");

const state = { origin: "", nickname: "", xhsUserId: "", previewed: false };

function setStatus(text, bad) {
  statusEl.textContent = text || "";
  statusEl.classList.toggle("bad", !!bad);
}

function refreshSend() {
  sendBtn.disabled = !(state.previewed && confirmBox.checked && state.origin);
}

function normalizeOrigin(raw) {
  let url;
  try {
    url = new URL((raw || "").trim());
  } catch (e) {
    throw new Error("请填写完整地址，例如 http://127.0.0.1:8000");
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error("只支持 http 或 https 的 Cortech Chat 地址");
  }
  return url.origin;
}

async function readOwnCookies() {
  // .com 在前：大陆和香港网页通常都是 www.xiaohongshu.com。
  // 只有浏览器里确实有 .cn / .hk 的登录 Cookie 时才会用到后面的域。
  const found = {};
  for (const domain of XHS_DOMAINS) {
    const rows = await chrome.cookies.getAll({ domain });
    for (const row of rows) {
      if (!ALLOWED.includes(row.name) || !row.value || found[row.name]) continue;
      found[row.name] = row.value;
    }
  }
  return found;
}

async function previewNickname() {
  setStatus("正在读取你浏览器里已有的登录态…");
  const jar = await readOwnCookies();
  const hasLogin = !!(jar.a1 && jar.web_session);
  if (!hasLogin) {
    state.previewed = false;
    previewBox.hidden = true;
    refreshSend();
    setStatus("没有找到你自己的小红书登录态。请先在这个浏览器打开小红书并登录，再点预览。大陆与香港一般都使用 www.xiaohongshu.com。", true);
    return;
  }
  let nickname = "";
  let xhsUserId = "";
  try {
    const res = await fetch("https://edith.xiaohongshu.com/api/sns/web/v1/user/selfinfo", {
      credentials: "include",
    });
    const data = await res.json();
    const info = (data && data.data) || data || {};
    nickname = info.nickname || "";
    xhsUserId = info.user_id || info.userid || info.red_id || "";
  } catch (e) {
    nickname = "";
  }
  state.nickname = nickname;
  state.xhsUserId = xhsUserId;
  state.previewed = true;
  previewBox.hidden = false;
  previewBox.textContent = nickname
    ? "将发送账号：「" + nickname + "」。Cookie 不会显示在这里。"
    : "已检测到你的登录态，但暂时读不到昵称。确认后仍会发送，Cookie 不会显示在这里。";
  refreshSend();
  setStatus("请勾选确认后再发送。");
}

async function cortechSession() {
  const cookie = await chrome.cookies.get({
    url: state.origin + "/",
    name: "cortech_session",
  });
  if (!cookie || !cookie.value) {
    throw new Error("这个浏览器还没有登录 Cortech Chat。请先打开上面的地址并登录，再回来确认发送。");
  }
  return cookie.value;
}

async function sendCookies() {
  sendBtn.disabled = true;
  setStatus("正在发送…");
  let jar = null;
  try {
    jar = await readOwnCookies();
    if (!jar.a1 || !jar.web_session) {
      throw new Error("登录态刚刚失效。请重新登录小红书后再预览。");
    }
    const session = await cortechSession();
    const headers = {
      "Authorization": "Bearer " + session,
      "Content-Type": "application/json",
    };
    const csrfRes = await fetch(state.origin + "/api/auth/csrf", { headers });
    const csrfBody = await csrfRes.json().catch(() => ({}));
    if (!csrfRes.ok || !csrfBody.csrf_token) {
      throw new Error(csrfBody.detail || "没有拿到安全校验码。请确认你已登录 Cortech Chat。");
    }
    headers["X-CSRF-Token"] = csrfBody.csrf_token;
    const payload = {
      a1: jar.a1,
      web_session: jar.web_session,
      nickname: state.nickname || "",
      xhs_user_id: state.xhsUserId || "",
      cookies: jar,
      confirm: true,
      enable: false,
    };
    const res = await fetch(state.origin + "/api/accounts/cookie-handshake", {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok || body.ok === false) {
      throw new Error(body.detail || body.message || "发送没有成功。");
    }
    previewBox.hidden = true;
    previewBox.textContent = "";
    confirmBox.checked = false;
    state.previewed = false;
    state.nickname = "";
    state.xhsUserId = "";
    setStatus(body.message || "已发送。登录态已从本页清除，屏幕上不再保留 Cookie。");
  } catch (e) {
    setStatus(e.message || "发送没有成功。", true);
  } finally {
    jar = null;
    refreshSend();
  }
}

document.getElementById("save-origin").addEventListener("click", async () => {
  try {
    const origin = normalizeOrigin(originInput.value);
    const granted = await chrome.permissions.request({ origins: [origin + "/*"] });
    if (!granted) {
      setStatus("需要你允许插件访问这个 Cortech Chat 地址。", true);
      return;
    }
    state.origin = origin;
    await chrome.storage.local.set({ cortechOrigin: origin });
    setStatus("已记住地址。请先在这个浏览器登录 Cortech Chat。");
  } catch (e) {
    setStatus(e.message || "地址不正确。", true);
  }
  refreshSend();
});

document.getElementById("preview").addEventListener("click", () => {
  previewNickname();
});

confirmBox.addEventListener("change", refreshSend);

sendBtn.addEventListener("click", () => {
  if (!confirmBox.checked || !state.previewed) return;
  sendCookies();
});

chrome.storage.local.get("cortechOrigin", (saved) => {
  if (saved && saved.cortechOrigin) {
    state.origin = saved.cortechOrigin;
    originInput.value = saved.cortechOrigin;
  }
});
