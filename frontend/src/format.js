export function num(value) {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 100000000) return `${(n / 100000000).toFixed(1)}亿`;
  if (abs >= 10000) return `${(n / 10000).toFixed(abs >= 100000 ? 0 : 1)}万`;
  return Number.isInteger(n) ? String(n) : n.toFixed(2);
}

export function money(value) {
  if (value === null || value === undefined || value === "") return "—";
  return `¥${Number(value).toLocaleString("zh-CN")}`;
}

export function pct(value) {
  if (value === null || value === undefined || value === "") return "—";
  return `${(Number(value) * 100).toFixed(2)}%`;
}

export function when(ts) {
  if (!ts) return "发布时间未知";
  const d = new Date(Number(ts) * 1000);
  if (Number.isNaN(d.getTime())) return "发布时间未知";
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export const REGION_OPTIONS = [
  { id: "", label: "全部地区" },
  { id: "cn", label: "大陆" },
  { id: "hk", label: "香港" },
  { id: "overseas", label: "海外 RED" },
];
