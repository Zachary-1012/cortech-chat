# -*- coding: utf-8 -*-
"""项目配置"""
from __future__ import annotations

import os
from pathlib import Path

# 项目根目录：以本文件位置推导，在任何机器、任何解压路径下都正确
# （禁止硬编码开发机路径）
BASE_DIR = Path(__file__).resolve().parent.parent

# Cortech 采集引擎目录（历史 vendor 路径，登录缓存不再依赖它）
CORTECH_VENDOR_DIR = BASE_DIR / "vendor" / "Cortech采集引擎"

MONITOR_DIR = BASE_DIR / "monitor"
STATIC_DIR = MONITOR_DIR / "static"
NEGATIVE_WORDS_FILE = MONITOR_DIR / "negative_words.txt"


def _load_dotenv() -> None:
    """加载 monitor/.env（KEY=VALUE，每行一个；已存在的环境变量不覆盖）。

    本地密钥文件，gitignored。绝不在日志/接口/文档中输出其内容。
    """
    env_file = MONITOR_DIR / ".env"
    if not env_file.exists():
        return
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip("'").strip('"')
            if k and v and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass  # .env 缺失/损坏不影响启动


_load_dotenv()


def resolve_data_dir() -> Path:
    """数据目录：CORTECH_DATA_DIR，其次历史别名 TKH_DATA_DIR，否则 <项目>/data。

    生产环境应把库、登录缓存和密钥放到程序目录之外。
    """
    raw = (os.environ.get("CORTECH_DATA_DIR") or os.environ.get("TKH_DATA_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return BASE_DIR / "data"


# 数据目录（SQLite / 采集运行目录 / 登录缓存 / 本地密钥）
DATA_DIR = resolve_data_dir()
RUNS_DIR = DATA_DIR / "runs"
DB_PATH = DATA_DIR / "monitor.db"


def bind_host() -> str:
    """监听地址。CORTECH_HOST 优先，其次 XHS_HOST，默认 0.0.0.0（容器 / VPS）。"""
    return (os.environ.get("CORTECH_HOST") or os.environ.get("XHS_HOST") or "0.0.0.0").strip() or "0.0.0.0"


def bind_port() -> int:
    """监听端口。CORTECH_PORT 优先，其次 XHS_PORT，默认 8000。"""
    raw = (os.environ.get("CORTECH_PORT") or os.environ.get("XHS_PORT") or "8000").strip()
    try:
        port = int(raw)
    except ValueError:
        return 8000
    if port < 1 or port > 65535:
        return 8000
    return port


def collection_fixture_enabled() -> bool:
    """为真时采集走本地样例，不访问小红书。仅用于闭环自检。"""
    return (os.environ.get("CORTECH_COLLECTION_FIXTURE") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def cookie_handshake_enabled() -> bool:
    """用户主动提交本人 Cookie 的握手。默认关闭。"""
    return (os.environ.get("CORTECH_ENABLE_COOKIE_HANDSHAKE") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def browser_use_enabled() -> bool:
    """用户指定本机已登录 profile 的可选钩子。默认关闭，不代登录。"""
    return (os.environ.get("CORTECH_ENABLE_BROWSER_USE") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )

# 默认采集参数
DEFAULT_MAX_NOTES = 50                 # 每个关键词最多采集帖子数
DEFAULT_ENABLE_COMMENTS = True         # 默认采集一级评论
DEFAULT_MAX_COMMENTS_PER_NOTE = 50     # 每帖最多评论数
DEFAULT_HEADLESS = False               # 浏览器是否无头（扫码登录需要可见窗口）
DEFAULT_TIMEOUT_SEC = 30 * 60          # 单次采集任务超时（秒）
DEFAULT_CRON = "0 2 * * *"             # 默认每天凌晨 2 点定时采集

# 预置话题（应用首次启动自动创建）
# 注意：以下为示例数据（太古汇是第一个用户）。换品牌时在「话题管理」页删除/修改即可，
# 或直接改这里后删库重建。业务逻辑中不 hardcode 品牌。
PRESET_TOPICS = [
    {
        "name": "广州太古汇十五周年",
        "keywords": "广州太古汇十五周年,太古汇十五周年",
        "max_notes": 100,
        "enable_comments": 1,
        "cron": "0 2 * * *",
        "engine": "cortech_direct",
    },
    {
        "name": "广州太古汇十五周年负面舆情",
        "keywords": "广州太古汇十五周年,太古汇15周年,太古汇十五周年",
        "max_notes": 50,
        "enable_comments": 1,
        "cron": "0 2 * * *",
        "engine": "cortech_direct",
    },
    {
        "name": "广州太古汇整体负面舆情",
        "keywords": "广州太古汇,太古汇避雷,太古汇投诉",
        "max_notes": 80,
        "enable_comments": 1,
        "cron": "0 2 * * *",
        "engine": "cortech_direct",
    },
]


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    login_cache_dir().mkdir(parents=True, exist_ok=True)


def _dir_has_entries(path: Path) -> bool:
    try:
        return path.exists() and any(path.iterdir())
    except OSError:
        return False


def login_cache_dir() -> Path:
    """可写的小红书登录缓存，放在数据目录下，不要求位于程序目录。"""
    return DATA_DIR / "xhs_login_cache"


def legacy_login_cache_dir() -> Path:
    """历史登录缓存位置。只作为读取回退，新登录不再写到这里。"""
    return CORTECH_VENDOR_DIR / "xhs_user_data_dir"


def active_login_cache_dir() -> Path:
    """读取登录缓存：优先数据目录，其次历史 vendor 目录。"""
    primary = login_cache_dir()
    if _dir_has_entries(primary):
        return primary
    legacy = legacy_login_cache_dir()
    if _dir_has_entries(legacy):
        return legacy
    return primary


# ---------------- AI 配置（多 Provider，OpenAI 兼容协议） ----------------
# 三种使用模式：
#  1. 自填 key（推荐）：Groq / Google Gemini，任意 OpenAI-compatible 网点；
#     默认优先级 Groq → Gemini，失败自动顺延
#  2. Brio 免费网关（备用）：因沙箱网络限制当前不可达，保留为手动备选
#  3. 规则兜底：网关不可达/未配置 key 时，基础指令走规则解析，保证不断线
# 安全：api_key 只存本地 data/ai_config.json 或环境变量；GET 接口永不返回明文 key；
#       日志中 key 一律打码。
AI_PROVIDERS_DEFAULT = {
    "brio": {
        "title": "Brio 免费网关（备用·当前网络不可达）",
        "base_url": "http://<brio-gateway-ip>:<port>/v1",
        "api_key": "__BRIO_KEY__",
        "model": "qwen2.5",
    },
    "groq": {
        "title": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": "",
        "model": "qwen/qwen3.8-27b",
    },
    "gemini": {
        "title": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key": "",
        "model": "gemini-2.0-flash",
    },
    "custom": {
        "title": "自定义",
        "base_url": "",
        "api_key": "",
        "model": "",
    },
}
AI_ACTIVE_DEFAULT = "groq"
# provider 自动顺延优先级：Groq（用户 key）→ Gemini（用户 key）→ 规则兜底。
# Brio 网关因沙箱网络限制当前不可达，仅作手动备选，不在自动链中。
AI_PROVIDER_PRIORITY = ["groq", "gemini"]
AI_TIMEOUT_SEC = int(os.environ.get("XHS_AI_TIMEOUT", "60"))
AI_CONFIG_FILE = DATA_DIR / "ai_config.json"   # 前端设置页持久化 AI 配置

# 各 provider 预设常用模型（/v1/models 拉不到时用这个；聊天窗口模型选择器用）
PRESET_MODELS = {
    "brio": ["qwen2.5:3b"],
    "groq": ["qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"],
    "gemini": ["gemini-2.5-flash", "gemini-2.5-flash-lite"],
    "custom": [],
}


def get_ai_config() -> dict:
    """返回 {active, providers:{name:{title,base_url,api_key,model}}}。

    优先级：data/ai_config.json > 环境变量（XHS_AI_BASE_URL/KEY/MODEL 覆盖 active provider）> 默认。
    """
    import copy
    providers = copy.deepcopy(AI_PROVIDERS_DEFAULT)
    active = AI_ACTIVE_DEFAULT
    # 各 provider 专用环境变量（monitor/.env 已在 import 时加载，作为"默认 key"来源）：
    # GROQ_API_KEY / GEMINI_API_KEY / BRIO_API_KEY，
    # 以及 <PREFIX>_AI_BASE_URL / <PREFIX>_AI_MODEL。
    # 先应用 env，再读 ai_config.json——用户在设置页显式保存的值优先于 .env 默认。
    for pname, prefix in (("brio", "BRIO"), ("groq", "GROQ"), ("gemini", "GEMINI"),
                          ("custom", "CUSTOM")):
        for k, env in (("base_url", f"{prefix}_AI_BASE_URL"),
                       ("api_key", f"{prefix}_API_KEY"),
                       ("model", f"{prefix}_AI_MODEL")):
            if os.environ.get(env):
                providers[pname][k] = os.environ[env]
    try:
        if AI_CONFIG_FILE.exists():
            import json
            saved = json.loads(AI_CONFIG_FILE.read_text(encoding="utf-8"))
            if saved.get("active") in providers:
                active = saved["active"]
            for name, p in (saved.get("providers") or {}).items():
                if name in providers and isinstance(p, dict):
                    for k in ("base_url", "api_key", "model"):
                        # 空字符串 = 未设置（save_ai_config 约定：空 key 表示不修改），
                        # 不能覆盖 .env/环境变量提供的默认值
                        if p.get(k):
                            providers[name][k] = p[k]
    except Exception:
        pass
    # XHS_AI_BASE_URL / XHS_AI_API_KEY / XHS_AI_MODEL：显式注入，覆盖当前生效 provider
    #（容器/交付场景优先级最高）
    env_map = {"base_url": "XHS_AI_BASE_URL", "api_key": "XHS_AI_API_KEY", "model": "XHS_AI_MODEL"}
    for k, env in env_map.items():
        if os.environ.get(env):
            providers[active][k] = os.environ[env]
    return {"active": active, "providers": providers}


def _env_provider_defaults() -> dict:
    """仅由默认+环境变量（.env）得出的 provider 配置，用于识别"来自 env 的 key"。"""
    import copy
    providers = copy.deepcopy(AI_PROVIDERS_DEFAULT)
    for pname, prefix in (("brio", "BRIO"), ("groq", "GROQ"), ("gemini", "GEMINI"),
                          ("custom", "CUSTOM")):
        for k, env in (("base_url", f"{prefix}_AI_BASE_URL"),
                       ("api_key", f"{prefix}_API_KEY"),
                       ("model", f"{prefix}_AI_MODEL")):
            if os.environ.get(env):
                providers[pname][k] = os.environ[env]
    env_map = {"base_url": "XHS_AI_BASE_URL", "api_key": "XHS_AI_API_KEY",
               "model": "XHS_AI_MODEL"}
    return providers


def get_active_provider() -> dict:
    """返回当前生效 provider 的 {name,title,base_url,api_key,model}"""
    cfg = get_ai_config()
    p = cfg["providers"][cfg["active"]]
    return {"name": cfg["active"], **p}


def save_ai_config(active: str | None = None, providers: dict | None = None) -> dict:
    """持久化 AI 配置。providers 中 api_key 为空字符串表示不修改已存 key。

    安全：来自环境变量/.env 的 key 不落盘写回 ai_config.json（保持为空，
    下次启动仍从 env 读取），只有用户在设置页显式提交的 key 才持久化。
    """
    import json
    ensure_dirs()
    cfg = get_ai_config()
    if active and active in cfg["providers"]:
        cfg["active"] = active
    env_defs = _env_provider_defaults()
    for name, p in (providers or {}).items():
        if name not in cfg["providers"] or not isinstance(p, dict):
            continue
        for k in ("base_url", "model"):
            if p.get(k) is not None:
                cfg["providers"][name][k] = p[k]
        if p.get("api_key"):  # 空字符串 = 保持原 key，避免前端误清空
            cfg["providers"][name]["api_key"] = p["api_key"]
    # 写回前：把"等于 env 默认值且本次未显式提交"的 key 清空，避免 env key 落盘
    explicit = {n for n, p in (providers or {}).items()
                if isinstance(p, dict) and p.get("api_key")}
    for name in cfg["providers"]:
        if (name not in explicit
                and cfg["providers"][name]["api_key"]
                and cfg["providers"][name]["api_key"] == env_defs[name]["api_key"]):
            cfg["providers"][name]["api_key"] = ""
    AI_CONFIG_FILE.write_text(
        json.dumps({"active": cfg["active"], "providers": cfg["providers"]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    return get_ai_config()


def mask_key(key: str) -> str:
    # 安全红线：绝不输出 key 的任何片段（前后缀都可能被用于推测）
    return "****" if key else ""


def public_ai_config() -> dict:
    """给前端的安全视图：不含明文 key，只给 has_key"""
    cfg = get_ai_config()
    return {
        "active": cfg["active"],
        "providers": [
            {"name": n,
             "title": p["title"],
             "base_url": p["base_url"],
             "model": p["model"],
             "has_key": bool(p["api_key"] and "__BRIO_KEY__" not in p["api_key"]
                             and "<brio-gateway" not in p["api_key"])}
            for n, p in cfg["providers"].items()
        ],
    }


def _is_placeholder(v: str) -> bool:
    return not v or "<" in v or ">" in v or v.strip() in ("__BRIO_KEY__",)
