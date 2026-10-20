# -*- coding: utf-8 -*-
"""Cortech Chat · Windows 桌面客户端

一个双击即用的桌面版入口，等价于「本地服务 + 应用窗口」：

  1. 后台拉起 FastAPI 服务（默认 127.0.0.1:8000，窗口不可见）；
  2. 以「应用模式」打开 Edge / Chrome —— 没有地址栏和标签页，就是一个独立 App 窗口；
  3. 关闭该窗口后，自动停掉后台服务并退出。

用法：
    双击 start-cortech.bat          （推荐，无控制台窗口）
    或  python CortechChat.py       （前台运行，便于排查）

若 8000 端口上已经有服务在跑，则直接复用，不会重复启动、退出时也不会误杀它。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIBS = ROOT / "libs"
DATA = ROOT / "data"
PROFILE_DIR = DATA / "desktop-profile"   # 独立浏览器 profile：窗口独立、登录态独立
LOG_FILE = DATA / "desktop.log"

HOST = os.environ.get("XHS_HOST", "127.0.0.1")
PORT = int(os.environ.get("XHS_PORT", "8000"))
URL = f"http://{HOST}:{PORT}"

# Windows: 子进程不弹控制台窗口
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def child_env() -> dict:
    """子进程环境：优先使用仓库内置的离线依赖 libs/。"""
    env = dict(os.environ)
    if LIBS.exists():
        env["PYTHONPATH"] = str(LIBS) + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONNOUSERSITE"] = "1"
    env["XHS_HOST"] = HOST
    env["XHS_PORT"] = str(PORT)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def server_ready(timeout: float = 1.5) -> bool:
    """探测服务是否已就绪。"""
    try:
        with urllib.request.urlopen(f"{URL}/api/auth/status", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_for_server(proc: subprocess.Popen, timeout: float = 120.0) -> bool:
    """等待服务就绪；子进程中途退出则立即失败。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if server_ready():
            return True
        if proc.poll() is not None:
            log(f"服务进程提前退出，返回码 {proc.returncode}，详见 {LOG_FILE}")
            return False
        time.sleep(0.4)
    log(f"等待服务就绪超时（{timeout:.0f}s）")
    return False


def find_browser() -> Path | None:
    """按 Edge → Chrome 顺序查找 Chromium 内核浏览器（支持应用窗口模式）。"""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidates.append(
            str(Path(local) / "Microsoft" / "Edge" / "Application" / "msedge.exe"))
        candidates.append(
            str(Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    for c in candidates:
        p = Path(c)
        if p.exists():
            return p
    return None


def open_app_window() -> subprocess.Popen | None:
    """以应用窗口模式打开前端。返回浏览器进程；无法使用时回退到默认浏览器。"""
    browser = find_browser()
    if not browser:
        log("未找到 Edge / Chrome，改用系统默认浏览器打开")
        if os.name == "nt":
            os.startfile(URL)  # noqa: S606
        else:
            import webbrowser
            webbrowser.open(URL)
        return None

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(browser),
        f"--app={URL}",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1500,950",
        "--disable-features=Translate",
    ]
    log(f"启动应用窗口：{browser.name}")
    return subprocess.Popen(cmd, creationflags=_NO_WINDOW)


def preflight() -> bool:
    """预检：确认当前解释器能加载依赖（内置 libs/ 是按某个 Python 版本编译的）。

    避免出现「窗口一闪而过、什么提示都没有」的情况。
    """
    probe = "import pydantic_core, bcrypt, fastapi, uvicorn, jieba, snownlp; print('ok')"
    try:
        r = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(ROOT), env=child_env(), capture_output=True,
            text=True, timeout=90, creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        log("依赖预检超时（90s），继续尝试启动")
        return True
    if r.returncode == 0:
        return True
    ver = ".".join(str(v) for v in sys.version_info[:3])
    log(f"依赖预检失败。当前解释器：{sys.executable}（Python {ver}）")
    log("内置依赖 libs/ 是按特定 Python 版本编译的（本项目为 3.14）。")
    log("请安装并使用对应版本的 Python，或执行：")
    log("    python -m pip install -r monitor/requirements.txt")
    for line in (r.stderr or "").strip().splitlines()[-4:]:
        log("  " + line)
    return False


def notify(title: str, message: str) -> None:
    """无控制台时（pythonw 启动）用系统弹窗提示，避免用户看不到任何反馈。"""
    log(f"[提示] {title}: {message}")
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x40)
    except Exception:
        pass


def stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    log("正在停止后台服务…")
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        log("服务未在 8s 内退出，强制结束")
        proc.kill()
        proc.wait(timeout=5)


def main() -> int:
    log("=" * 56)
    log("Cortech Chat 桌面客户端启动中…")

    if not (ROOT / "monitor" / "app.py").exists():
        notify("Cortech Chat 启动失败",
               f"未找到 monitor/app.py，请确认启动器位于项目根目录：\n{ROOT}")
        return 1
    if not LIBS.exists():
        log("提示：未发现内置依赖目录 libs/，将使用系统已安装的 Python 依赖")
        if not preflight():
            notify("Cortech Chat 启动失败",
                   f"依赖不完整。\n请执行：python -m pip install -r monitor/requirements.txt\n\n"
                   f"详细日志：{LOG_FILE}")
            return 1

    server: subprocess.Popen | None = None
    if server_ready():
        log(f"检测到 {URL} 已有服务在运行，直接复用（退出时不会停止它）")
    else:
        server = subprocess.Popen(
            [sys.executable, "-m", "monitor.app"],
            cwd=str(ROOT), env=child_env(), creationflags=_NO_WINDOW,
        )
        log(f"后台服务已拉起（PID {server.pid}），等待就绪…")
        if not wait_for_server(server):
            stop_server(server)
            notify("Cortech Chat 启动失败",
                   f"后台服务未能就绪，请查看日志：\n{LOG_FILE}")
            return 1
        log(f"服务就绪：{URL}")

    browser = open_app_window()
    try:
        if browser is not None:
            log("应用窗口已打开，关闭窗口即退出。")
            browser.wait()
        else:
            log(f"已在浏览器中打开 {URL}，按 Ctrl+C 退出。")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        log("收到退出信号")
    finally:
        if server is not None:
            stop_server(server)
        log("Cortech Chat 已退出")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # 兜底：把异常写进日志，避免窗口一闪而过
        log(f"启动失败：{type(exc).__name__}: {exc}")
        import traceback
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"error": str(exc)}, ensure_ascii=False) + "\n")
                f.write(traceback.format_exc() + "\n")
        except OSError:
            pass
        sys.exit(1)
