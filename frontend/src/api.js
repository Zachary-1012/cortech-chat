export async function api(path, { method = "GET", body, query } = {}) {
  const url = new URL(`/api${path}`, window.location.origin);
  if (query) {
    Object.entries(query).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== "") {
        url.searchParams.set(key, value);
      }
    });
  }
  const res = await fetch(url, {
    method,
    credentials: "include",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { detail: text };
  }
  if (res.status === 401 && !window.location.pathname.startsWith("/login")) {
    const next = encodeURIComponent(window.location.pathname + window.location.search);
    window.location.href = `/login?redirect=${next}`;
    throw new Error("未登录");
  }
  if (!res.ok) {
    const detail = data && (data.detail || data.message);
    const msg = typeof detail === "string" ? detail : res.statusText;
    throw new Error(msg || "请求失败");
  }
  return data;
}
