# Cortech Chat 域名部署

使用者只打开浏览器。Python 依赖装在服务器或镜像里，不要求对方的 Windows 电脑执行 `pip`。

数据目录放在镜像和程序目录外面（`CORTECH_DATA_DIR`）。库、登录态和密钥都在这里。

探活：`GET /health` 或 `GET /api/health`。带登录 Cookie 的 `GET /api/setup/status` 还会返回登录组件、健康小红书账号数、是否已有话题、最近一次采集。

## 容器

在仓库根目录：

```bash
docker compose -f deploy/docker-compose.yml up -d --build
curl -fsS http://127.0.0.1:8000/health
```

编排里已经写了 `CORTECH_SAAS=1` 和数据目录 `/var/lib/cortech`。AI key 等可选变量写进 `deploy/env.production`（从 `deploy/env.production.example` 复制，该文件不要提交），再在 compose 的 `cortech-chat` 服务下加上 `env_file: ./env.production`。

Compose 把数据放在命名卷 `cortech_data`，对应容器内 `/var/lib/cortech`。前面再加 `deploy/nginx/cortech-chat.conf`，把 `YOUR_DOMAIN` 和证书路径换成真实值。

`CORTECH_SAAS=1` 已写在编排里。本人 Cookie 握手因此默认开启。

## systemd + nginx

适合不跑 Docker 的云主机。

```bash
sudo useradd --system --home /var/lib/cortech --create-home cortech || true
sudo mkdir -p /opt/cortech-chat /var/lib/cortech
sudo rsync -a ./ /opt/cortech-chat/
cd /opt/cortech-chat
python3 -m venv .venv
.venv/bin/pip install -r monitor/requirements.txt
sudo cp deploy/env.production.example /etc/cortech-chat.env
sudo cp deploy/cortech-chat.service /etc/systemd/system/cortech-chat.service
sudo systemctl daemon-reload
sudo systemctl enable --now cortech-chat
curl -fsS http://127.0.0.1:8000/health
```

然后安装 `deploy/nginx/cortech-chat.conf`，替换域名和证书占位符，`nginx -t && systemctl reload nginx`。

## 样例采集和真实会话

| 路径 | 怎么开 | 会不会访问小红书 |
|------|--------|------------------|
| 样例闭环 | `CORTECH_COLLECTION_FIXTURE=1`，再对某个话题点采集 | 不会。任务进度到 100，帖子列表和总览出现本地样例 |
| 真实会话 | 关掉上面的开关。账号管理里用本人小红书扫码，或用 `extensions/cortech-xhs-assist` 确认后发送自己的登录态 | 会。没有有效登录态时接口返回 `need_xhs_session` |

插件可以打成 zip：`./extensions/build-zip.sh`。
