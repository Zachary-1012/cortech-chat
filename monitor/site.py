# -*- coding: utf-8 -*-
"""域名绑定（自助接入）：填域名自动配置 + 接入状态自检 + 换域名备份。

流程：用户在设置页输入域名 → save_domain() 自动完成：
  1. 更新 CORS 白名单（site_config.json + 应用重启/热加载生效）
  2. 记录前端 API 基地址（默认相对 /api；独立部署时前端构建用 VITE_API_BASE_URL）
  3. 生成该域名的 nginx 配置文件 data/nginx/<domain>.conf（含 80→443 跳转）
  4. 给出 certbot 一键 HTTPS 命令
DNS 解析仍需用户在域名商处操作（客观限制）；配好后点"检测接入"做三步自检：
  DNS 解析 → 比对本机公网 IP → HTTPS 访问本站。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import time
from pathlib import Path
from typing import Dict, List, Optional

from .config import BASE_DIR, DATA_DIR, bind_port, ensure_dirs
from .netutil import http_client, public_ip

logger = logging.getLogger("xhs-monitor.site")

SITE_CONFIG_FILE = DATA_DIR / "site_config.json"
NGINX_DIR = DATA_DIR / "nginx"
NGINX_BACKUP_DIR = NGINX_DIR / "backup"

_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$")


def validate_domain(domain: str) -> str:
    d = (domain or "").strip().lower().rstrip(".")
    if not d or not _DOMAIN_RE.match(d):
        raise ValueError(f"域名格式非法: {domain!r}")
    return d


def get_site_config() -> Dict:
    ensure_dirs()
    cfg = {"domain": "", "cors_origins": [], "api_base": "/api",
           "backend_port": bind_port(),
           # 品牌档案（通用化：网站是通用的，品牌只是配置项）
           # 默认值为通用示例，换品牌时在设置页修改即可，无需改代码
           "brand_name": "示例品牌",
           "brand_short": "示例",
           "updated_at": 0, "history": []}
    try:
        if SITE_CONFIG_FILE.exists():
            saved = json.loads(SITE_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                cfg.update({k: v for k, v in saved.items() if k in cfg})
    except Exception:
        pass
    # 环境变量可追加 CORS
    extra = os.environ.get("XHS_CORS_ORIGINS", "")
    if extra:
        for o in [x.strip() for x in extra.split(",") if x.strip()]:
            if o not in cfg["cors_origins"]:
                cfg["cors_origins"].append(o)
    return cfg


def save_site_config(domain: str = "", cors_origins: Optional[List[str]] = None,
                     api_base: str = "/api") -> Dict:
    """保存站点配置并生成 nginx 配置（含旧配置备份）。返回配置 + 生成产物路径。"""
    ensure_dirs()
    cfg = get_site_config()
    old_domain = cfg.get("domain", "")
    domain = validate_domain(domain) if domain else ""
    if cors_origins is None:
        cors_origins = [f"https://{domain}", f"http://{domain}"] if domain else []
    cfg.update({"domain": domain, "cors_origins": cors_origins,
                "api_base": api_base or "/api", "updated_at": int(time.time())})
    if old_domain and old_domain != domain:
        cfg["history"].append({"domain": old_domain,
                               "replaced_at": int(time.time())})
        cfg["history"] = cfg["history"][-10:]
    SITE_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    nginx_path = None
    if domain:
        nginx_path = str(generate_nginx_conf(domain, old_domain or None))
    return {"config": public_site_config(),
            "nginx_conf": nginx_path,
            "certbot_cmd": certbot_command(domain) if domain else ""}


def public_site_config() -> Dict:
    cfg = get_site_config()
    return {k: v for k, v in cfg.items() if k != "history"} | \
        {"history": [{"domain": h["domain"], "replaced_at": h["replaced_at"]}
                     for h in cfg.get("history", [])]}


def get_brand() -> Dict[str, str]:
    """品牌档案：name（全称）/ short（简称）。换品牌改配置即可，不改代码。"""
    cfg = get_site_config()
    return {"name": cfg.get("brand_name") or "示例品牌",
            "short": cfg.get("brand_short") or "示例"}


def save_brand(name: str, short: str) -> Dict[str, str]:
    """更新品牌档案。"""
    name = (name or "").strip()
    short = (short or "").strip()
    if not name:
        raise ValueError("品牌名称不能为空")
    if not short:
        raise ValueError("品牌简称不能为空")
    ensure_dirs()
    cfg = get_site_config()
    cfg["brand_name"] = name
    cfg["brand_short"] = short
    cfg["updated_at"] = int(time.time())
    SITE_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    return get_brand()


NGINX_TEMPLATE = """# 由 Chat Cortech 自动生成（{domain}），{ts}
# 用法：sudo cp {conf_name} /etc/nginx/sites-available/ && \\
#       sudo ln -s /etc/nginx/sites-available/{conf_name} /etc/nginx/sites-enabled/ && \\
#       sudo nginx -t && sudo systemctl reload nginx
# HTTP → HTTPS 跳转
server {{
    listen 80;
    server_name {domain};
    return 301 https://$host$request_uri;
}}

# HTTPS 反代（前端静态资源由后端托管；也可把 frontend/dist 交给 nginx 直服）
server {{
    listen 443 ssl;
    server_name {domain};

    # certbot 会自动填入下面两行；手动证书请替换为实际路径
    ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;

    client_max_body_size 50m;

    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }}
}}
"""


def generate_nginx_conf(domain: str, old_domain: Optional[str] = None) -> Path:
    """生成 nginx 配置；旧域名配置备份到 backup/。"""
    import datetime as _dt
    NGINX_DIR.mkdir(parents=True, exist_ok=True)
    NGINX_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if old_domain:
        old_conf = NGINX_DIR / f"{old_domain}.conf"
        if old_conf.exists():
            bak = NGINX_BACKUP_DIR / f"{old_domain}.{int(time.time())}.conf.bak"
            shutil.copy2(old_conf, bak)
            logger.info("旧域名 nginx 配置已备份：%s", bak)
    conf = NGINX_DIR / f"{domain}.conf"
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conf.write_text(NGINX_TEMPLATE.format(
        domain=domain, ts=ts, conf_name=conf.name,
        port=get_site_config()["backend_port"]), encoding="utf-8")
    return conf


def certbot_command(domain: str) -> str:
    return (f"sudo certbot --nginx -d {domain} "
            f"--non-interactive --agree-tos --redirect "
            f"-m admin@{domain}  # 邮箱请按实际修改")


class DynamicCORSMiddleware:
    """动态 CORS：每次请求读取 site_config.json 的白名单，保存域名后即时生效。

    未配置域名时仅允许同源；本地开发端口（localhost:5173 等）默认放行。
    """

    DEV_ORIGINS = {
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:3000", "http://127.0.0.1:3000",
    }

    def __init__(self, app):
        self.app = app

    def _allowed(self, origin: str) -> bool:
        if not origin:
            return False
        if origin in self.DEV_ORIGINS:
            return True
        return origin in get_site_config().get("cors_origins", [])

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        origin = headers.get(b"origin", b"").decode("latin1")
        allowed = self._allowed(origin)

        if scope["method"] == "OPTIONS" and origin:
            # 预检请求：直接 204 返回（不再进路由）
            async def preflight_send(message):
                await send(message)

            if allowed:
                await preflight_send({
                    "type": "http.response.start", "status": 204,
                    "headers": [
                        (b"access-control-allow-origin", origin.encode("latin1")),
                        (b"access-control-allow-methods",
                         b"GET, POST, PUT, PATCH, DELETE, OPTIONS"),
                        (b"access-control-allow-headers",
                         b"Authorization, Content-Type"),
                        (b"access-control-max-age", b"600"),
                        (b"vary", b"Origin"),
                    ],
                })
            else:
                await preflight_send({
                    "type": "http.response.start", "status": 204, "headers": [],
                })
            await preflight_send({"type": "http.response.body", "body": b""})
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start" and allowed:
                h = dict(message.get("headers") or [])
                h[b"access-control-allow-origin"] = origin.encode("latin1")
                h[b"vary"] = b"Origin"
                message["headers"] = list(h.items())
            await send(message)

        await self.app(scope, receive, send_wrapper)


def check_domain(domain: str) -> Dict:
    """接入状态自检：DNS 解析 → 本机公网 IP 比对 → HTTPS 访问本站。"""
    domain = validate_domain(domain)
    result: Dict = {"domain": domain, "overall": False, "checks": {}}

    # 1. DNS 解析
    dns_ok, resolved, dns_msg = False, [], ""
    try:
        infos = socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)
        resolved = sorted({i[4][0] for i in infos})
        dns_ok = bool(resolved)
        dns_msg = f"解析到 {', '.join(resolved)}" if dns_ok else "未解析到任何 IP"
    except socket.gaierror as e:
        dns_msg = f"DNS 未生效：{e}（请检查域名商解析是否已添加 A 记录并等待生效）"
    result["checks"]["dns"] = {"ok": dns_ok, "ips": resolved, "message": dns_msg}

    # 2. 本机公网 IP 比对
    pip = public_ip()
    ip_ok = bool(dns_ok and pip and pip in resolved)
    if not pip:
        ip_msg = "无法获取本机公网 IP（出口受限），请人工比对解析 IP 是否为本服务器"
    elif pip in resolved:
        ip_msg = f"解析 IP 与本机公网 IP 一致（{pip}）"
    else:
        ip_msg = (f"解析 IP {', '.join(resolved)} 与本机公网 IP {pip} 不一致："
                  f"域名可能指到了别的服务器，请检查 A 记录")
    result["checks"]["ip_match"] = {"ok": ip_ok, "public_ip": pip, "message": ip_msg}

    # 3. HTTPS 访问本站
    https_ok, https_msg, status = False, "", None
    try:
        with http_client(15.0) as c:
            r = c.get(f"https://{domain}/api/topics", follow_redirects=True)
            status = r.status_code
            https_ok = r.status_code < 500
            https_msg = (f"HTTPS 访问正常（HTTP {r.status_code}）" if https_ok
                         else f"HTTPS 返回异常（HTTP {r.status_code}）：请检查 nginx 与证书")
    except Exception as e:  # noqa: BLE001
        en = type(e).__name__
        if "CERTIFICATE" in str(e).upper() or "SSL" in en.upper():
            https_msg = f"证书异常（{en}）：请确认 certbot 已签发且 nginx 已重载"
        elif "Connect" in en or "Timeout" in en:
            https_msg = f"端口不通（{en}）：请检查云厂商安全组是否放行 80/443、nginx 是否运行"
        else:
            https_msg = f"HTTPS 访问失败（{en}）：{str(e)[:80]}"
    result["checks"]["https"] = {"ok": https_ok, "status": status,
                                 "message": https_msg}

    result["overall"] = dns_ok and ip_ok and https_ok
    result["verdict"] = ("绿灯：域名已接入" if result["overall"]
                         else "红灯：接入未完成，请按上面各步骤提示修复")
    return result
