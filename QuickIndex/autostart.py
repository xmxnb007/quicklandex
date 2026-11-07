# -*- coding: utf-8 -*-
"""QuickIndex 开机自启 / 进程管理

    python autostart.py --install    写入当前用户开机启动项（无需管理员）+ 立即静默启动
    python autostart.py --uninstall  移除开机启动项
    python autostart.py --status     查看状态
    python autostart.py --launch     立即静默启动（已在运行则只唤起窗口）
    python autostart.py --show       唤起搜索窗口
    python autostart.py --quit       退出常驻
    python autostart.py --scan       快速增量刷新
    python autostart.py --deep       完整增量扫描
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import winreg

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(APP_DIR, "daemon.py")
CONFIG = os.path.join(APP_DIR, "data", "config.json")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "QuickIndex"
IPC_PORT = 8766

DESIRED_CONFIG = {
    "auto_index_on_start": False,   # 启动零扫描
    "scan_interval_minutes": 15,    # 后台快速增量间隔
    "hotkey": "ctrl+alt+f",
    "hide_on_blur": False,          # 贴附面板默认不因失焦收起
    "show_tray": True,
    "attach_to_explorer": True,
    "auto_scope_explorer": True,
    "keep_attached_on_blur": True,
    "compact_bar": True,
    "frameless_bar": True,
    "expand_on_type": True,
    "follow_move": True,
    "follow_active_explorer": True,
    "auto_scope_follow": True,
    "allow_manual_scope": True,
    "hide_after_open": True,
    "enable_hotkey": True,
    "enable_autostart": True,
    "enable_explorer_menu": True,
}


def pythonw() -> str:
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        return exe
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.isfile(cand):
        return cand
    launcher = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                            "Programs", "Python", "Launcher", "pyw.exe")
    if os.path.isfile(launcher):
        return launcher
    return exe


def launch_command() -> str:
    pw = pythonw()
    if os.path.basename(pw).lower() == "pyw.exe":
        return f'"{pw}" -3 "{DAEMON}"'
    return f'"{pw}" "{DAEMON}"'


def write_config() -> None:
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    cfg = {}
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            pass
    # 只补缺失项，绝不覆盖用户在「设置」里改过的开关
    for k, v in DESIRED_CONFIG.items():
        cfg.setdefault(k, v)
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def ipc(cmd: str) -> bool:
    try:
        c = socket.create_connection(("127.0.0.1", IPC_PORT), timeout=1.2)
        c.sendall(cmd.upper().encode("ascii"))
        c.settimeout(1.2)
        c.recv(16)
        c.close()
        return True
    except OSError:
        return False


def is_running() -> bool:
    return ipc("STATUS")


def launch() -> bool:
    if is_running():
        return ipc("SHOW")
    flags = 0x08000000 | 0x00000008  # CREATE_NO_WINDOW | DETACHED_PROCESS
    pw = pythonw()
    args = [pw, DAEMON] if os.path.basename(pw).lower() != "pyw.exe" else [pw, "-3", DAEMON]
    subprocess.Popen(args, cwd=APP_DIR, creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True)
    return True


def autostart_enabled() -> bool:
    """启动项是否存在且未被「任务管理器 → 启动应用」禁用。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            val, _ = winreg.QueryValueEx(key, VALUE_NAME)
        if not val:
            return False
    except FileNotFoundError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as key:
            raw, _ = winreg.QueryValueEx(key, VALUE_NAME)
        # 二进制首字节：02/06 = 已启用，03 = 已禁用
        if isinstance(raw, bytes) and raw and raw[0] in (3, 0):
            return False
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return True


def set_autostart(enabled: bool) -> None:
    """开关开机自启（只改注册表，不动当前进程），并清掉「已禁用」标记。"""
    if enabled:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, launch_command())
        _clear_disabled_flag()
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, VALUE_NAME)
        except FileNotFoundError:
            pass


def _clear_disabled_flag() -> None:
    """任务管理器里若曾手动禁用过，删掉禁用标记让启动项真正生效。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass
    except OSError:
        pass


def install() -> None:
    write_config()
    set_autostart(True)
    print("  已加入开机自启（当前用户，无需管理员）")
    print("  命令：", launch_command())
    print("  热键： Ctrl+Alt+F 唤起搜索窗口")
    print("  启动零扫描；后台每 15 分钟做一次快速增量")
    if not is_running():
        launch()
        print("  已立即静默启动")
    else:
        ipc("SHOW")
        print("  常驻进程已在运行，已唤起窗口")


def uninstall() -> None:
    set_autostart(False)
    print("  已移除开机自启")


def restart() -> None:
    """退出当前常驻并用最新配置重新静默拉起（设置改动需重启时用）。"""
    ipc("QUIT")
    import time
    for _ in range(20):
        time.sleep(0.15)
        if not is_running():
            break
    launch()


def status() -> None:
    print("  开机自启：", "已开启（未被禁用）" if autostart_enabled() else "未开启 / 已禁用")
    print("  常驻进程：", "运行中" if is_running() else "未运行")
    cfg = {}
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            pass
    print("  配置：", json.dumps(cfg, ensure_ascii=False))
    log = os.path.join(APP_DIR, "data", "daemon.log")
    if os.path.exists(log):
        with open(log, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-6:]
        print("  最近日志：")
        for line in lines:
            print("   ", line.rstrip())


def main() -> None:
    # pythonw.exe / DETACHED_PROCESS 下 stdout 为 None，print 会抛 lost sys.stdout
    if sys.stdout is None:
        import io
        sys.stdout = io.StringIO()
    p = argparse.ArgumentParser(description="QuickIndex 开机自启管理")
    p.add_argument("--install", action="store_true")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--status", action="store_true")
    p.add_argument("--launch", action="store_true")
    p.add_argument("--show", action="store_true")
    p.add_argument("--quit", action="store_true")
    p.add_argument("--scan", action="store_true")
    p.add_argument("--deep", action="store_true")
    p.add_argument("--enable", action="store_true", help="只写开机启动项")
    p.add_argument("--disable", action="store_true", help="只移除开机启动项")
    p.add_argument("--restart", action="store_true", help="重启常驻进程")
    p.add_argument("--settings", action="store_true", help="打开设置窗口")
    a = p.parse_args()

    if a.settings:
        print("  已打开设置" if ipc("SETTINGS") else "  常驻进程未运行，请先运行 启动.bat")
    elif a.enable:
        set_autostart(True)
        print("  开机自启：已开启")
    elif a.disable:
        set_autostart(False)
        print("  开机自启：已关闭")
    elif a.restart:
        restart()
        print("  常驻进程已重启")
    elif a.install:
        install()
    elif a.uninstall:
        uninstall()
    elif a.launch:
        write_config()
        launch()
        print("  已启动 / 唤起")
    elif a.show:
        print("  已唤起" if ipc("SHOW") else "  常驻进程未运行，请先运行 启动.bat")
    elif a.quit:
        ipc("QUIT")
        print("  已请求退出")
    elif a.scan:
        print("  已触发快速增量" if ipc("SCAN") else "  常驻进程未运行")
    elif a.deep:
        print("  已触发完整增量" if ipc("DEEP") else "  常驻进程未运行")
    else:
        status()


if __name__ == "__main__":
    main()
