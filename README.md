# Cortech Chat

> 小红书高强采集 · 社媒运营 · 整合营销的 Web SaaS
>
> 让社媒运营有数可依。

主路径是浏览器里的 Web SaaS：服务跑在云主机或容器上，使用者只打开网页。
内置 SQLite。本地双击脚本仍可用于演示，不是交付方式。

---

## 快速开始

### 云主机 / VPS / 容器（主路径）

在服务器上安装一次依赖，之后用环境变量启动。终端用户不需要在自己的电脑上 `pip install`。

```bash
python3 -m pip install -r monitor/requirements.txt
export CORTECH_DATA_DIR=/var/lib/cortech    # 库、登录缓存、密钥；不要放在程序目录里
export CORTECH_HOST=0.0.0.0
export CORTECH_PORT=8000
python3 -m monitor.app
```

可选的多进程启动（需另装 gunicorn）：

```bash
gunicorn monitor.app:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000
```

打开 `http://<服务器>:8000`。首次注册后，到「账号管理」用**本人**小红书账号扫码。没有真实登录态时，采集接口会返回 `need_xhs_session`，不会假装采到了笔记。

自检：

```bash
curl -s http://127.0.0.1:8000/api/setup/status
```

返回 JSON，包含数据库、数据目录可写、bcrypt 登录组件、以及当前用户是否已有小红书账号。

| 变量 | 作用 | 默认 |
|------|------|------|
| `CORTECH_DATA_DIR` | 数据目录（`TKH_DATA_DIR` 仍可作为别名） | 项目下 `data/` |
| `CORTECH_HOST` | 监听地址（`XHS_HOST` 为旧名） | `0.0.0.0` |
| `CORTECH_PORT` | 监听端口（`XHS_PORT` 为旧名） | `8000` |
| `CORTECH_SAAS` | `1` 表示云上 SaaS。未单独设置握手开关时，**默认开启**本人 Cookie 握手 | 关 |
| `CORTECH_COLLECTION_FIXTURE` | `1` 时写入本地样例，不访问小红书 | 关 |
| `CORTECH_ENABLE_COOKIE_HANDSHAKE` | `1` 开启 / `0` 关闭本人 Cookie 握手。未设置时跟随 `CORTECH_SAAS` | 跟随 SaaS |
| `CORTECH_ENABLE_BROWSER_USE` | `1` 时才允许复用你已经登录的 profile。不代登录 | 关 |
| `CORTECH_BROWSER_USE_PROFILE` | 你已经登录小红书的目录（含 `storage_state.json` 或浏览器 profile） | 空 |
| `CORTECH_BROWSER_USE_CONCURRENCY` | 同时使用该会话的采集数 | `1` |
| `CORTECH_BROWSER_USE_QUEUE` | 排队等待的上限 | `4` |
| `CORTECH_ACCOUNT_DAILY_QUOTA` | 每个小红书账号每天计入的采集条数 | `300` |
| `CORTECH_ACCOUNT_SKIP_SEC` | 登录失效后暂停轮换的秒数 | `1800` |

云上建议同时设置：

```bash
export CORTECH_SAAS=1
```

这样本人 Cookie 握手默认开启。若要关掉，显式写 `CORTECH_ENABLE_COOKIE_HANDSHAKE=0`。

负载均衡探活用 `GET /health`（也可用 `GET /api/health`）。`GET /api/setup/status` 在带登录 Cookie 时返回本账号的：登录组件是否就绪、健康小红书账号数、是否已有话题、最近一次采集状态。

**样例闭环与真实会话**

- 样例：`CORTECH_COLLECTION_FIXTURE=1` 后，对话题点采集。任务进度会走到 100，帖子列表和总览出现本地样例。服务不会访问小红书。
- 真实会话：关掉该开关。在「账号管理」用本人小红书扫码，或用浏览器插件确认后发送自己的登录态。没有有效登录态时，采集返回 `need_xhs_session`，不会编造笔记。

域名上线的容器和主机样例在 [`deploy/README.md`](deploy/README.md)：`docker compose`，或 systemd + nginx（证书路径是占位符）。数据目录放在镜像外面。使用网页的人不用在自己的 Windows 上安装 Python 依赖。

域名接入仍可用「设置」里的站点配置生成 nginx 样例。证书和 DNS 需要你在域名服务商处完成。

### 浏览器插件：把自己的小红书登录态发给 Cortech Chat

仓库里有 Chrome / Edge MV3 扩展 `extensions/cortech-xhs-assist/`。它只在你点击并确认后，读取**你这个浏览器里已经登录**的小红书 Cookie，发给你已经登录的 Cortech Chat。不会在后台静默读取，也不会登录别人的账号。

1. 打开 Chrome 或 Edge 的扩展管理页，打开「开发者模式」。
2. 「加载已解压的扩展程序」，选中 `extensions/cortech-xhs-assist/`。
3. 在插件里填写 Cortech Chat 地址（例如 `http://127.0.0.1:8000` 或你的 https 域名），点保存，并允许访问该地址。
4. 用同一个浏览器先登录 Cortech Chat。服务器上需要握手可用：`CORTECH_SAAS=1`，或 `CORTECH_ENABLE_COOKIE_HANDSHAKE=1`。
5. 在这个浏览器打开小红书并登录自己的号，再点插件里的「预览我的昵称」。确认昵称后勾选确认，才会发送。
6. 发送成功后，页面上不再保留 Cookie。服务端加密保存，并立刻做登录态健康检查。

大陆和香港访问小红书网页，通常都是 `www.xiaohongshu.com`，Cookie 域名是 `.xiaohongshu.com`。插件同时识别 `.xiaohongshu.cn` 和 `.xiaohongshu.hk`：只有当你的浏览器里确实有这些域名的登录 Cookie 时才会读取。采集器读 Cookie 时会同时询问这三个网页域，以及 `edith.xiaohongshu.com` 上的 host-only Cookie；同名时采用 `.com`。签名请求仍然发到 `edith.xiaohongshu.com`。应用商店上架以后再补，现在用「加载已解压的扩展程序」。

打成 zip：macOS / Linux 用 `./extensions/build-zip.sh`，Windows 用 `powershell -File extensions\build-zip.ps1`。压缩包根目录就是插件文件，解压后用「加载已解压的扩展程序」。

可选：把你已经登录好的浏览器目录指给 `CORTECH_BROWSER_USE_PROFILE`，并设置 `CORTECH_ENABLE_BROWSER_USE=1`。采集会复用这份会话，排队和并发有上限。目录里没有 `a1` 和 `web_session` 时会拒绝，服务端不会代为登录。

### Windows（本地演示：桌面窗口）

1. 安装 **Python 3.14**（勾选 *Add Python to PATH*）
   <https://www.python.org/downloads/>
2. 双击 **`start-cortech.bat`**

   - **首次运行自动把依赖装到 `libs/`**（一次性，需联网；约 250MB）；此后完全离线可用；
   - 后台拉起服务（`127.0.0.1:8000`），并以**无地址栏的应用窗口**打开界面；
   - **关闭窗口即自动停止服务**；
   - 停止/排查：`stop-cortech.bat`，运行日志见 `data/desktop.log`。

3. 首次运行会引导**注册账号**（本地版无需邮箱验证码）；首个注册用户自动继承内置示例数据。
4. 可选：双击 `创建桌面快捷方式.bat`，在桌面生成带图标的 Cortech Chat 快捷方式。

> 本机直接跑服务：`python -m monitor.app`（默认 `0.0.0.0:8000`），浏览器打开 <http://127.0.0.1:8000>。云上请改用上面的环境变量。

### Windows 本地 SaaS（不打开桌面窗口）

双击 **`start-saas.bat`**。它只做三件事：设置 `CORTECH_SAAS=1`、把数据目录设为项目下的 `data\`、运行 `python -m monitor.app`。不会打开桌面窗口。`libs\` 里已经有依赖时，这个脚本不会安装包。

浏览器打开 <http://127.0.0.1:8000> 。关闭该命令行窗口即停止服务。

### Linux / macOS

```bash
./start-cortech.sh    # 首次运行同样自动装依赖到 libs/（需 Python 3.10+）
./stop-cortech.sh     # 停止
```

**系统要求**：内存 4GB+（推荐 8GB），磁盘 500MB+（含依赖），采集与 AI 功能需要联网。

> **依赖说明**：`libs/` 是离线依赖目录，体积较大，**不进 git 仓库**（见 `.gitignore`），
> 由启动脚本在首次运行时自动生成。若需手动安装：
> `python -m pip install --target libs -r monitor/requirements.txt`。

---

## 功能模块（17 个）

| # | 模块 | 说明 |
|---|------|------|
| 1 | 总览 | 核心指标卡、情感分布、声量趋势、负面预警、快捷入口 |
| 2 | 话题管理 | 话题增删改、自动扩展约 20 个相关 tag、新 tag 自动吸收、启停与一键全量采集 |
| 3 | 帖子列表 | 按话题/时间/情感/关键词筛选，趋势图 → 列表 → 详情三层下钻 |
| 4 | 时间线 | 按**发布时间**展示演变；对数/线性切换、头部/腰部/尾部分档、平均数与中位数 |
| 5 | 热词分析 | 总量榜/增量榜，7/30/90/365 天，点击热词联动筛选 |
| 6 | 事件聚类 | 自动识别舆情事件，事件时间线、关键帖子、情感走向 |
| 7 | 传播路径图谱 | 传播趋势 → 关键节点（源头/放大器/桥梁）→ 人群画像 |
| 8 | 达人库 | 达人档案、相似达人推荐、按话题关联达人 |
| 9 | 竞品对标 | 多品牌声量对比、SOV 份额、影响力评分 |
| 10 | 预警中心 | 三层预警（负面突增/敏感词/声量异常）、历史与已读状态 |
| 11 | 伪冒反查 | 品牌档案、变体词矩阵、8 大判定模式、定时巡检、举报材料与证据链 |
| 12 | 全网资讯 | 新闻搜索 + RSS 聚合、去重与情感打标、热点式时间线叠加小红书声量 |
| 13 | 内容创作中心 | 图文/短视频脚本/活动推文/海报文案/直播预告/评论话术，AI 生成 + 在线编辑 + 换一版 |
| 14 | 方案比稿 | PDF/DOCX 上传解析、AI 六维拆解、横向对比表 + 四维雷达、评审报告 |
| 15 | AI 聊天 | 自然语言问答、快捷入口按钮、可点击链接、聊天记录持久化、顶部模型切换 |
| 16 | 一键全量采集 | 话题下所有 tag 一次采回，实时进度、断点续采 |
| 17 | 设置 / 模型配置 | AI Provider 配置、域名绑定、采集账号管理 |

**核心交互**：扫码登录小红书、从本地浏览器导入已登录账号（需用户二次确认，只显示昵称/头像，
绝不暴露 cookie）、用浏览器插件确认后发送本人登录态、多账号按健康状态和每日配额轮换。

---

## 用户账号与数据隔离

- 邮箱注册 / 登录；密码 **bcrypt**（cost 12）加密，服务端 session 管理；
- 所有业务表按 `user_id` 过滤，用户间数据完全隔离；
- 未登录访问 `/api/*` 返回 **401**；首个注册用户自动继承内置种子数据。

---

## 数据规范（采集铁律）

- **采集时间 ≠ 发布时间**：发布时间从接口毫秒时间戳解析为北京时间并保留原值；
  采集时间为入库时间戳。前端两行分别展示，导出保留两列。
- 趋势 / 时间线 / 排序一律用**发布时间**，任务记录用**采集时间**；
- 解析失败标「未知」，**不混入趋势**。

---

## 技术架构

```
monitor/                    FastAPI 后端（采集 / 分析 / AI / 伪冒反查 模块化）
  app.py                    175+ 路由的 API 层
  adapters/                 采集引擎适配器（direct / browser / spider / downloader）
  db.py                     SQLite 数据层（参数化 SQL + 用户隔离）
  fakecheck.py              伪冒反查（判定模式库、嫌疑账号、举报材料）
  seed_data/                内置示例数据
frontend/                  Vue 3 + Vite 源码（`src/`、`package.json`）
frontend/dist/              `npm run build` 的产物，由后端托管
CortechChat.py              Windows 桌面客户端启动器
```

- **后端**：Python + FastAPI + SQLite（参数化 SQL、无注入；XSS 防护）
- **前端**：Vue 3 + Vite。源码在 `frontend/src`，页面路由覆盖总览、话题、帖子、时间线、热词、事件、达人、竞品、预警、伪冒反查、资讯、创作、比稿、对话、设置、登录和账号。
- **AI**：多 Provider（Groq / Gemini / 自定义 OpenAI 兼容网点），失败自动顺延 + **规则兜底**
- **采集**：小红书网页端 direct-API，登录态复用，失效时降级浏览器引擎
- **安全**：登录态 Fernet 加密（密钥 600 权限）；API key 只存本地、不入库不入日志

### 前端构建

页面源码在 `frontend/`。改完界面后重新构建，后端会托管 `frontend/dist/`。资源前缀是 `/v3-admin-vite/`，页面路由在站点根路径。

```bash
cd frontend
npm install
npm run build
```

构建产物写到 `frontend/dist/`。然后 `python3 -m monitor.app`，打开 <http://127.0.0.1:8000>。开发时也可以 `npm run dev`（Vite 把 `/api` 代理到 8000 端口）。

达人预估、港版/海外地区和主体核验都不调用付费接口：预估公式写在接口返回的 `formulas` 里；关键词繁简用 OpenCC（`opencc-python-reimplemented`）；主体核验用 GB 32100、Wikidata、信用中国和公示系统公开页，打不开就降级。

---

## 配置

AI key 有两种配置方式，效果等同：

1. **设置页**（推荐）：界面「设置 → 模型配置」填写并保存（落盘到 `data/ai_config.json`）；
2. **本地密钥文件**：

   ```bash
   copy monitor\.env.example monitor\.env     # Windows
   cp monitor/.env.example monitor/.env       # Linux / macOS
   ```

   `monitor/.env` 已被 `.gitignore` 忽略，**不会进入仓库**；`GET /api/ai-config` 永不返回明文 key。

---

## 内置示例数据

| 数据 | 内容 |
|------|------|
| 广州太古汇十五周年 | 85 条帖子（11 条负面标记）、20 个扩展 tag |
| DFS 伪冒 | 48 个嫌疑账号、57 个变体词矩阵、70 条特征 |

均为**示例数据**，可在界面中删除或替换为自己的业务数据（业务逻辑中零品牌硬编码）。

---

## 自检

```bash
# 全链路验证：采集引擎 / 时间铁律 / 情感 / 趋势 / 报告 / 导出 / AI / 去重
set PYTHONPATH=libs && python -m monitor.verify_all       # Windows
PYTHONPATH=libs python -m monitor.verify_all              # Linux / macOS
```

---

## 许可

[MIT](LICENSE)
