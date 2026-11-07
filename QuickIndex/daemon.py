# -*- coding: utf-8 -*-
"""QuickIndex 常驻后台主程序

启动方式：pythonw.exe daemon.py（无控制台、无窗口，静默驻留）
    · 启动零扫描：直接复用已有索引，开机即刻可用
    · Ctrl+Alt+F 全局热键 / 托盘左键 / 再次运行启动.bat  → 唤起搜索窗口
    · 每 15 分钟后台做一次「目录时间戳剪枝」快速增量，不逐文件重扫
    · 单实例：第二个进程只发一条 SHOW 消息后退出
    · 贴附资源管理器：一打开就是顶部一条无边框长条；输入后展开结果
    · 进入文件夹自动更新搜索范围，也可手动选择 / 锁定范围
"""
from __future__ import annotations

import os
import queue
import socket
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk, filedialog

from engine import APP_DIR, Indexer, human_size
import winui
try:
    import shellpath  # 资源管理器窗口路径 / 位置感知
except Exception:  # pragma: no cover - 感知失败不影响主功能
    shellpath = None
try:
    import autostart
except Exception:  # pragma: no cover
    autostart = None
try:
    import explorer as explorer_ext
except Exception:  # pragma: no cover
    explorer_ext = None

IPC_PORT = 8766
FOLLOW_MS = 10             # 跟随刷新间隔 ≈100fps，只走 Win32，不触发 Tk 重排
ANCHOR_WATCH_S = 0.22      # 检测「当前活动的资源管理器窗口」间隔
SCOPE_WATCH_S = 0.5        # 检测资源管理器当前目录变化的间隔（后台线程）
MOVE_GRACE = 1.2           # 窗口移动 / 缩放后的自动隐藏宽限期（秒）
ATTACH_TOP_OFFSET = 62     # 贴附在资源管理器地址栏下方
BAR_H = 52                 # 长条形高度
BAR_MARGIN = 10            # 长条相对资源管理器左右缩进
FONT = ("Microsoft YaHei UI", 10)
FONT_SMALL = ("Microsoft YaHei UI", 9)
DATA_DIR = os.path.join(APP_DIR, "data")
LOG_PATH = os.path.join(DATA_DIR, "daemon.log")
PID_PATH = os.path.join(DATA_DIR, "daemon.pid")

TYPES = [("全部", ""), ("文档", "document"), ("图片", "image"), ("视频", "video"),
         ("音频", "audio"), ("压缩包", "archive"), ("代码", "code"), ("文件夹", "dir")]
SORTS = [("相关度", "relevance"), ("最近修改", "mtime"), ("文件名", "name"), ("文件大小", "size")]


def log(msg: str) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%m-%d %H:%M:%S] ") + msg + "\n")
    except OSError:
        pass


def excepthook(exc_type, exc, tb) -> None:
    log("未捕获异常：\n" + "".join(traceback.format_exception(exc_type, exc, tb)))


sys.excepthook = excepthook


def fmt_time(ts: float) -> str:
    if not ts:
        return "—"
    d = time.time() - ts
    if d < 60:
        return "刚刚"
    if d < 3600:
        return f"{int(d // 60)} 分钟前"
    if d < 86400:
        return f"{int(d // 3600)} 小时前"
    if d < 86400 * 30:
        return f"{int(d // 86400)} 天前"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def normcase(path: str) -> str:
    return os.path.normcase(os.path.normpath(path)) if path else ""


class QuickIndexApp:
    def __init__(self):
        self.indexer = Indexer()
        self.cfg = self.indexer.config
        self.q: queue.Queue = queue.Queue()
        self.items: list[dict] = []
        self.gen = 0
        self.visible = False
        self.debounce_id = None
        self.scope = ""                # 当前搜索范围（由资源管理器传入 / 自动感知 / 手动选择）
        self.scope_follow = bool(self.cfg.get("auto_scope_follow", True))  # 运行时是否跟随文件夹
        self.anchor_hwnd = 0           # 贴附的资源管理器窗口
        self.anchor_rect: tuple[int, int, int, int] | None = None
        self.follow_pos: tuple[int, int] | None = None
        self.panel_hwnd = 0            # 面板自己的顶层 HWND
        self.last_move_ts = 0.0
        self._hide_check_at = 0.0
        self._anchor_watch_at = 0.0
        self._modal_until = 0.0
        self._size_save_id = None
        self._last_status_text = ""
        self.pid = os.getpid()
        self.panel_w, self.panel_h = 960, 560
        self.user_size: tuple[int, int] | None = None
        self.screen_w, self.screen_h = 0, 0
        self._topmost = True
        self.frameless = False         # 当前是否已去掉标题栏
        self.expanded = True           # 当前是否展开结果面板
        self._setting_view = False     # 程序化切换形态期间忽略 Configure
        self._settings_win = None
        self.compact_bar = bool(self.cfg.get("compact_bar", True))
        # 记住用户上次手动调整的展开面板大小
        try:
            pw = int(self.cfg.get("panel_width", 0) or 0)
            ph = int(self.cfg.get("panel_height", 0) or 0)
            if 480 <= pw <= 2400 and 300 <= ph <= 1600:
                self.user_size = (pw, ph)
                self.panel_w, self.panel_h = pw, ph
        except (TypeError, ValueError):
            pass
        self.next_scan = time.time() + max(1, int(self.cfg.get("scan_interval_minutes", 15))) * 60

        self._start_ipc()

        self.root = tk.Tk()
        self.root.title("QuickIndex · 本地文件极速搜索")
        self.root.withdraw()
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.bind("<Configure>", self._on_configure)
        self.root.bind("<Escape>", lambda e: self.hide())
        self.root.resizable(True, True)
        self._build_ui()
        self._center()

        winui.start(
            self.cfg.get("hotkey", "ctrl+alt+f"), self.q, LOG_PATH,
            show_tray=bool(self.cfg.get("show_tray", True)),
            enable_hotkey=bool(self.cfg.get("enable_hotkey", True)),
            icon_path=os.path.join(APP_DIR, "icon.ico"),
        )
        with open(PID_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        log(f"常驻启动完成，索引 {self.indexer.stats()['total']:,} 条，启动零扫描")

        # 用户主动关掉「零扫描」时，启动后做一次快速增量（仍然不逐文件全扫）
        if self.cfg.get("auto_index_on_start", False):
            try:
                self.indexer.start_index(deep=False)
            except Exception:
                pass

        self.root.after(120, self._tick)
        self.root.after(FOLLOW_MS, self._follow_tick)
        self.root.after(300, lambda: self.run_search(initial=True))
        threading.Thread(target=self._scope_watch_loop, name="qi-scope", daemon=True).start()
        self.root.mainloop()

    # ------------------------------------------------------------ 单实例 / IPC
    def _start_ipc(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv.bind(("127.0.0.1", IPC_PORT))
        except OSError:
            try:
                c = socket.create_connection(("127.0.0.1", IPC_PORT), timeout=1)
                c.sendall(b"SHOW")
                c.close()
            except OSError:
                pass
            log("已有实例在运行，本次仅唤起窗口")
            sys.exit(0)
        srv.listen(8)

        def loop():
            while True:
                try:
                    conn, _ = srv.accept()
                    data = conn.recv(8192).decode("utf-8", "ignore").strip()
                    try:
                        conn.sendall(b"OK")
                    finally:
                        conn.close()
                    if data:
                        self.q.put(("ipc", data))
                except OSError:
                    break

        threading.Thread(target=loop, name="quickindex-ipc", daemon=True).start()

    # ------------------------------------------------------------ 界面
    def _build_ui(self):
        self.root.configure(bg="#f4f6fa")
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#f4f6fa")
        style.configure("TLabel", background="#f4f6fa", foreground="#5b6472", font=FONT_SMALL)
        style.configure("TButton", font=FONT_SMALL, padding=(7, 3))
        style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff",
                        foreground="#1c212b", rowheight=25, borderwidth=0, font=FONT)
        style.configure("Treeview.Heading", background="#e9edf5", foreground="#556074",
                        font=(FONT[0], 9, "bold"), borderwidth=0, padding=(6, 5))
        style.map("Treeview", background=[("selected", "#d7e3ff")],
                  foreground=[("selected", "#18387f")])

        # 顶栏：长条形态下唯一可见的一行
        self.top = tk.Frame(self.root, bg="#f4f6fa")
        self.top.pack(fill="x", padx=12, pady=(12, 8))

        self.var_q = tk.StringVar()
        self.var_q.trace_add("write", lambda *_: self._on_change())
        self.entry = tk.Entry(self.top, textvariable=self.var_q, font=("Microsoft YaHei UI", 14),
                              relief="flat", bg="#ffffff", fg="#1c212b",
                              insertbackground="#4361ee", highlightthickness=2,
                              highlightcolor="#4361ee", highlightbackground="#dfe4ee")
        self.entry.bind("<Return>", self._on_return)
        self.entry.bind("<Control-Return>", lambda e: self._open_selected(reveal=True))
        self.entry.bind("<Up>", lambda e: self._move(-1))
        self.entry.bind("<Down>", lambda e: self._move(1))
        self.entry.bind("<Escape>", lambda e: self.hide())

        hotkey = str(self.cfg.get("hotkey", "ctrl+alt+f")).upper()
        self.lbl_hotkey = tk.Label(self.top, text=hotkey, bg="#e6ebff", fg="#2f4bd6",
                                   font=FONT_SMALL, padx=9, pady=4)
        self.btn_settings = ttk.Button(self.top, text="设置", width=5, command=self._open_settings)
        self.btn_view = ttk.Button(self.top, text="展开", width=5, command=self._toggle_view)
        self.btn_scope = ttk.Button(self.top, text="范围", width=5, command=self._popup_scope_menu)
        # 范围标签放进固定容器，清除范围时只隐藏标签本身，不改变右侧按钮顺序
        self.scope_holder = tk.Frame(self.top, bg="#f4f6fa")
        self.lbl_scope = tk.Label(self.scope_holder, text="", bg="#e6ebff", fg="#2f4bd6",
                                  font=FONT_SMALL, padx=8, pady=3, cursor="hand2")
        self.lbl_scope.bind("<Button-1>", lambda e: self.clear_scope())
        self.lbl_scope.bind("<Button-3>", lambda e: self._popup_scope_menu(e))
        # 右侧按钮先占位，输入框最后填充剩余空间
        for w in (self.lbl_hotkey, self.btn_settings, self.btn_view, self.btn_scope,
                  self.scope_holder):
            w.pack(side="right", padx=(3, 0))
        self.entry.pack(side="left", fill="x", expand=True, ipady=9, padx=(0, 8))

        # 筛选行
        self.bar = tk.Frame(self.root, bg="#f4f6fa")
        self.bar.pack(fill="x", padx=12, pady=(0, 6))
        tk.Label(self.bar, text="类型", bg="#f4f6fa", fg="#7b8494", font=FONT_SMALL).pack(side="left")
        self.cmb_type = ttk.Combobox(self.bar, values=[t[0] for t in TYPES], state="readonly",
                                     width=9, font=FONT_SMALL)
        self.cmb_type.current(0)
        self.cmb_type.pack(side="left", padx=(4, 14))
        self.cmb_type.bind("<<ComboboxSelected>>", lambda e: self.run_search())
        tk.Label(self.bar, text="排序", bg="#f4f6fa", fg="#7b8494", font=FONT_SMALL).pack(side="left")
        self.cmb_sort = ttk.Combobox(self.bar, values=[s[0] for s in SORTS], state="readonly",
                                     width=9, font=FONT_SMALL)
        self.cmb_sort.current(0)
        self.cmb_sort.pack(side="left", padx=4)
        self.cmb_sort.bind("<<ComboboxSelected>>", lambda e: self.run_search())
        self.lbl_count = tk.Label(self.bar, text="", bg="#f4f6fa", fg="#7b8494", font=FONT_SMALL)
        self.lbl_count.pack(side="right")

        # 结果区
        self.mid = tk.Frame(self.root, bg="#f4f6fa")
        self.mid.pack(fill="both", expand=True, padx=12)
        cols = ("name", "size", "time", "dir")
        self.tree = ttk.Treeview(self.mid, columns=cols, show="headings", selectmode="browse")
        for c, title, width, anchor in (
            ("name", "名称", 290, "w"), ("size", "大小", 90, "e"),
            ("time", "修改时间", 110, "center"), ("dir", "所在目录", 400, "w"),
        ):
            self.tree.heading(c, text=title)
            self.tree.column(c, width=width, minwidth=50, anchor=anchor,
                             stretch=(c in ("name", "dir")))
        vs = ttk.Scrollbar(self.mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self._open_selected())
        self.tree.bind("<Return>", lambda e: self._open_selected())
        self.tree.bind("<Button-3>", self._context_menu)

        self.menu = tk.Menu(self.root, tearoff=0, font=FONT_SMALL)
        self.menu.add_command(label="打开文件", command=lambda: self._open_selected())
        self.menu.add_command(label="打开所在文件夹", command=lambda: self._open_selected(True))
        self.menu.add_command(label="复制完整路径", command=self._copy_path)
        self.menu.add_separator()
        self.menu.add_command(label="只搜索这个结果所在的目录",
                              command=self._scope_to_selected)
        self.menu.add_separator()
        self.menu.add_command(label="快速增量刷新", command=lambda: self._scan(False))
        self.menu.add_command(label="完整增量扫描", command=lambda: self._scan(True))

        # 范围菜单
        self.var_follow = tk.BooleanVar(value=self.scope_follow)
        self.scope_menu = tk.Menu(self.root, tearoff=0, font=FONT_SMALL)
        self.scope_menu.add_checkbutton(label="自动跟随当前文件夹", variable=self.var_follow,
                                        command=self._toggle_follow)
        self.scope_menu.add_separator()
        self.scope_menu.add_command(label="手动选择文件夹…", command=self._choose_scope)
        self.scope_menu.add_command(label="搜索上级目录", command=self._scope_parent)
        self.scope_menu.add_command(label="全盘搜索（清除范围）", command=self.clear_scope)
        self.scope_menu.add_command(label="只搜索选中结果所在目录",
                                    command=self._scope_to_selected)
        self.scope_menu.add_separator()
        self.scope_menu.add_command(label="复制当前范围路径", command=self._copy_scope)
        self.scope_menu.add_command(label="打开设置…", command=self._open_settings)

        # 底栏
        self.bottom = tk.Frame(self.root, bg="#f4f6fa")
        self.bottom.pack(fill="x", padx=12, pady=(8, 10))
        for text, cmd in (("快速刷新", lambda: self._scan(False)),
                          ("完整扫描", lambda: self._scan(True)),
                          ("设置", self._open_settings)):
            ttk.Button(self.bottom, text=text, command=cmd).pack(side="right", padx=3)
        self.lbl_status = tk.Label(self.bottom, text="就绪", bg="#f4f6fa", fg="#7b8494",
                                   font=FONT_SMALL, anchor="w")
        self.lbl_status.pack(side="left", fill="x", expand=True)

    # ------------------------------------------------------------ 形态 / 尺寸
    def _panel_size(self) -> tuple[int, int]:
        # 用户手动拖过就完全尊重；否则展开宽度与长条一致，输入时不会横向跳位
        if self.user_size:
            return self.user_size
        return (self._bar_width() if self.anchor_rect else self.panel_w), self.panel_h

    def _bar_width(self) -> int:
        if self.anchor_rect:
            w = int(self.anchor_rect[2]) - 2 * BAR_MARGIN
            return max(420, min(w, (self.screen_w or 1920) - 16))
        return self.panel_w

    def _current_size(self) -> tuple[int, int]:
        if self.compact_bar and not self.expanded:
            return self._bar_width(), BAR_H
        return self._panel_size()

    def _center(self):
        w, h = self._current_size()
        self.screen_w = self.root.winfo_screenwidth()
        self.screen_h = self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{max(0, (self.screen_w - w) // 2)}+{max(0, int(self.screen_h * 0.22))}")
        self.follow_pos = None

    def _center_pos(self, w: int, h: int) -> tuple[int, int]:
        sw = self.screen_w or self.root.winfo_screenwidth()
        sh = self.screen_h or self.root.winfo_screenheight()
        return max(0, (sw - w) // 2), max(0, int(sh * 0.22))

    def _target_pos(self, rect: tuple[int, int, int, int] | None) -> tuple[int, int]:
        """面板左上角目标位置：按当前形态宽度居中，并夹在屏幕范围内。"""
        pw, ph = self._current_size()
        sw = self.screen_w or self.root.winfo_screenwidth()
        sh = self.screen_h or self.root.winfo_screenheight()
        if not rect:
            return self._center_pos(pw, ph)
        left, top, ew, _eh = rect
        x = max(0, min(left + (ew - pw) // 2, max(0, sw - pw)))
        y = max(0, min(top + ATTACH_TOP_OFFSET, max(0, sh - ph - 8)))
        return x, y

    def _ensure_panel_hwnd(self) -> None:
        """拿到面板真正的顶层 HWND（winfo_id 在 Windows 上可能只是子窗口）。"""
        if shellpath is None:
            return
        try:
            hwnd = shellpath.top_level_hwnd(int(self.root.winfo_id()))
            if hwnd and shellpath.user32.IsWindow(shellpath.wintypes.HWND(hwnd)):
                self.panel_hwnd = hwnd
        except Exception:
            pass

    def _apply_view(self, keep_pos: bool = True) -> None:
        """在「无边框长条」与「可调整大小的结果面板」之间切换。

        两种形态共用同一个窗口与输入框，切换只做一次 geometry，
        跟随移动仍然只走 Win32 SetWindowPos，不会每帧重排控件。
        """
        self._setting_view = True
        w, h = self._current_size()
        if keep_pos and self.follow_pos:
            x, y = self.follow_pos
        elif self.visible:
            try:
                x, y = self.root.winfo_x(), self.root.winfo_y()
            except tk.TclError:
                x, y = self._target_pos(self.anchor_rect)
        else:
            x, y = self._target_pos(self.anchor_rect)

        want_frameless = bool(self.compact_bar and self.cfg.get("frameless_bar", True)
                              and not self.expanded)
        if want_frameless != self.frameless:
            try:
                self.root.overrideredirect(want_frameless)
                self.frameless = want_frameless
                self.panel_hwnd = 0        # 去掉/恢复边框可能重建 HWND
                if self.visible:
                    # Windows 上切换 overrideredirect 需要重新映射才能正确加/去掉标题栏
                    self.root.withdraw()
                    self.root.deiconify()
                self.root.attributes("-topmost", self._topmost)
            except tk.TclError:
                pass

        if self.expanded:
            self.top.pack_configure(padx=12, pady=(12, 8))
            self.entry.pack_configure(ipady=9)
            self.btn_view.configure(text="收起")
            for f, kw in ((self.bar, {"padx": 12, "pady": (0, 6)}),
                          (self.mid, {"padx": 12}),
                          (self.bottom, {"padx": 12, "pady": (8, 10)})):
                if not f.winfo_manager():
                    f.pack(fill="both" if f is self.mid else "x",
                           expand=bool(f is self.mid), **kw)
            self.root.resizable(True, True)
        else:
            self.top.pack_configure(padx=8, pady=5)
            self.entry.pack_configure(ipady=4)
            self.btn_view.configure(text="展开")
            for f in (self.bar, self.mid, self.bottom):
                if f.winfo_manager():
                    f.pack_forget()
            self.root.resizable(False, False)

        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.follow_pos = (x, y)
        try:
            self.root.update_idletasks()
        except tk.TclError:
            pass
        self._ensure_panel_hwnd()
        # Configure 事件是异步的，稍后再开放尺寸记忆，避免把程序化尺寸记成用户调整
        self.root.after(180, self._end_setting_view)

    def _end_setting_view(self):
        self._setting_view = False

    def _set_expanded(self, value: bool) -> None:
        if value == self.expanded:
            return
        self.expanded = value
        self._apply_view(keep_pos=True)
        if self.anchor_rect and self.panel_hwnd:
            x, y = self._target_pos(self.anchor_rect)
            if shellpath.move_window(self.panel_hwnd, x, y):
                self.follow_pos = (x, y)
        try:
            self.entry.focus_force()
            self.entry.icursor("end")
        except tk.TclError:
            pass
        log(f"切换形态 expanded={value} size={self._current_size()}")

    def _auto_expand(self) -> None:
        # 延后到变量 trace 之外再切换窗口形态，避免 Tcl 回调里重映射窗口
        if (self.visible and self.compact_bar and not self.expanded
                and self.cfg.get("expand_on_type", True)
                and self.var_q.get().strip()):
            self._set_expanded(True)

    def _toggle_view(self):
        self._set_expanded(not self.expanded)
        self.entry.focus_force()

    def _on_configure(self, event):
        """记录用户手动调整的展开面板尺寸；长条形 / 程序化切换不记录。"""
        if event.widget is not self.root or not self.visible or self._setting_view:
            return
        if not self.expanded:
            return
        w, h = int(event.width), int(event.height)
        if not (320 <= w <= 4000 and 200 <= h <= 3000) or (w, h) == self.user_size:
            return
        self.user_size = (w, h)
        self.panel_w, self.panel_h = w, h
        if self._size_save_id:
            try:
                self.root.after_cancel(self._size_save_id)
            except tk.TclError:
                pass
        self._size_save_id = self.root.after(900, self._save_panel_size)

    def _save_panel_size(self):
        self._size_save_id = None
        if not self.user_size:
            return
        try:
            self.indexer.save_config({"panel_width": self.user_size[0],
                                      "panel_height": self.user_size[1]})
        except Exception:
            pass

    # ------------------------------------------------------------ 显示 / 隐藏
    def show(self, detect: bool = True, from_explorer: bool = False):
        self.root.after(0, lambda: self._show_main(detect, from_explorer))

    def _pick_anchor(self, target: str = "") -> tuple[int, tuple[int, int, int, int] | None]:
        """只要存在可见的资源管理器窗口就返回 (hwnd, rect)，保证一打开自动贴上去。"""
        if shellpath is None:
            return 0, None
        fg = shellpath.foreground_explorer()
        if fg:
            return fg
        if target:
            try:
                hwnd = shellpath.explorer_hwnd_for_path(target)
                if hwnd:
                    rr = shellpath.window_rect(hwnd)
                    if rr:
                        return hwnd, rr
            except Exception:
                pass
        vis = shellpath.visible_explorers()
        return vis[0] if vis else (0, None)

    def _show_main(self, detect: bool = True, from_explorer: bool = False):
        # 已经显示：只提到最前，绝不重新 deiconify / 重设尺寸（这是「开第二个就消失」的根因之一）
        if self.visible:
            self.root.lift()
            self.root.attributes("-topmost", True)
            self._topmost = True
            self.entry.focus_force()
            self.entry.select_range(0, "end")
            self.last_move_ts = time.time()
            return

        if detect and shellpath is not None and self.cfg.get("auto_scope_explorer", True):
            try:
                path, _ = shellpath.foreground_path()
                self._apply_scope(path, refresh=False, follow=True)
            except Exception:
                pass
        self.anchor_hwnd = 0
        self.anchor_rect = None
        self.follow_pos = None
        rect = None
        if shellpath is not None and self.cfg.get("attach_to_explorer", True):
            try:
                self.anchor_hwnd, rect = self._pick_anchor(self.scope)
            except Exception:
                rect = None
        self.anchor_rect = rect
        if rect:
            self.screen_w = self.root.winfo_screenwidth()
            self.screen_h = self.root.winfo_screenheight()

        self.compact_bar = bool(self.cfg.get("compact_bar", True))
        q = self.var_q.get().strip()
        self.expanded = (not self.compact_bar) or (
            bool(self.cfg.get("expand_on_type", True)) and bool(q))
        # 坐标与形态必须在 deiconify 之前设置，否则窗口管理器会用旧坐标覆盖
        self._apply_view(keep_pos=False)
        self.root.deiconify()
        self.root.update_idletasks()
        self._ensure_panel_hwnd()
        if rect and self.panel_hwnd:
            x, y = self._target_pos(rect)
            if shellpath.move_window(self.panel_hwnd, x, y):
                self.follow_pos = (x, y)
        self.visible = True
        self.root.lift()
        self.root.attributes("-topmost", True)
        self._topmost = True
        if self.panel_hwnd and shellpath is not None:
            shellpath.activate_window(self.panel_hwnd)
        self.entry.focus_force()
        self.entry.select_range(0, "end")
        self.last_move_ts = time.time()
        self._render_scope()
        log(f"显示面板 detect={detect} from_explorer={from_explorer} "
            f"anchor={self.anchor_hwnd} rect={rect} bar={not self.expanded} "
            f"scope={self.scope!r} q={self.var_q.get()!r}")
        self.run_search(initial=True)

    def _reanchor(self, hwnd: int, rect: tuple[int, int, int, int]) -> None:
        """切到另一个资源管理器窗口：只换锚点 + 搬窗口，绝不隐藏面板。"""
        if not hwnd or hwnd == self.anchor_hwnd:
            return
        self.anchor_hwnd = hwnd
        self.anchor_rect = rect
        self.last_move_ts = time.time()
        x, y = self._target_pos(rect)
        if self.panel_hwnd and shellpath.move_window(self.panel_hwnd, x, y):
            self.follow_pos = (x, y)
        log(f"重新贴附到资源管理器窗口 hwnd={hwnd} rect={rect}")

    def _anchor_watch(self) -> None:
        """用户手动打开第二个资源管理器窗口时，面板跟过去而不是消失。"""
        if not self.visible or shellpath is None:
            return
        if not (self.cfg.get("attach_to_explorer", True)
                and self.cfg.get("follow_active_explorer", True)):
            return
        try:
            fg = shellpath.foreground_explorer()
            if fg and fg[0] != self.anchor_hwnd:
                self._reanchor(fg[0], fg[1])
                return
            if not self.anchor_hwnd:
                vis = shellpath.visible_explorers()
                if vis:
                    self._reanchor(vis[0][0], vis[0][1])
        except Exception:
            pass

    def _follow_tick(self):
        """≈100fps 轻量跟随：全程只调 Win32，不改尺寸、不重排控件、不抢焦点。"""
        try:
            now = time.time()
            if self.visible and self.anchor_hwnd and shellpath is not None:
                hwnd = shellpath.wintypes.HWND(self.anchor_hwnd)
                if not shellpath.user32.IsWindow(hwnd):
                    # 锚点窗口关掉了：重新找一个，找不到就保持面板可见（绝不 hide）
                    self.anchor_hwnd = 0
                    vis = shellpath.visible_explorers()
                    if vis:
                        self._reanchor(vis[0][0], vis[0][1])
                elif not shellpath.is_minimized(self.anchor_hwnd):
                    rect = shellpath.window_rect(self.anchor_hwnd)
                    if rect and rect != self.anchor_rect:
                        self.anchor_rect = rect
                        self.last_move_ts = now
                        if self.cfg.get("follow_move", True):
                            x, y = self._target_pos(rect)
                            if (x, y) != self.follow_pos and self.panel_hwnd:
                                if shellpath.move_window(self.panel_hwnd, x, y):
                                    self.follow_pos = (x, y)
                # 最小化时什么都不做：面板留在原地，等窗口恢复或切到别的窗口
            if self.visible and now - self._anchor_watch_at >= ANCHOR_WATCH_S:
                self._anchor_watch_at = now
                self._anchor_watch()
            if self.visible:
                self._auto_hide_check()
        except Exception:
            pass
        self.root.after(FOLLOW_MS if self.visible else 120, self._follow_tick)

    def _auto_hide_check(self):
        """贴附状态下永不自动隐藏；只有未贴附且用户明确开启失焦隐藏时才收起。"""
        now = time.time()
        if now < self._hide_check_at or now < self._modal_until:
            return
        self._hide_check_at = now + 0.12
        if shellpath is None:
            return
        hwnd, cls, pid = shellpath.foreground_info()
        attached = bool(self.cfg.get("attach_to_explorer", True))
        if attached and self.cfg.get("keep_attached_on_blur", True):
            # 移动、缩放、点选资源管理器、再开一个窗口都不会退出；
            # 切到其它程序只是让出「总在最上」，面板本身一直在，切回即恢复。
            want_top = (pid == self.pid or shellpath.is_explorer_class(cls))
            if want_top != self._topmost:
                self._topmost = want_top
                try:
                    self.root.attributes("-topmost", want_top)
                except tk.TclError:
                    pass
            return
        if not self.cfg.get("hide_on_blur", False):
            return
        if now - self.last_move_ts < MOVE_GRACE:
            return
        if not hwnd or pid == self.pid:
            return
        if cls in shellpath.TRANSIENT_CLASSES or shellpath.is_explorer_class(cls):
            return
        self.hide()

    def hide(self):
        if not self.visible:
            return
        self.visible = False
        self.anchor_hwnd = 0
        self.anchor_rect = None
        self.follow_pos = None
        self._save_panel_size()
        self.root.withdraw()

    # ------------------------------------------------------------ 搜索范围
    def _apply_scope(self, path: str, refresh: bool = True, follow: bool | None = None) -> None:
        self.scope = os.path.normpath(path) if path and os.path.isdir(path) else ""
        if follow is not None:
            self.scope_follow = bool(follow)
            try:
                self.var_follow.set(self.scope_follow)
            except Exception:
                pass
        self._render_scope()
        if refresh:
            self.run_search()

    def _render_scope(self) -> None:
        if self.scope:
            short = os.path.basename(self.scope) or self.scope
            tag = "" if self.scope_follow else " · 手动"
            text = f"范围：{short}{tag}   ✕"
            self.lbl_scope.configure(text=text)
            if not self.lbl_scope.winfo_manager():
                self.lbl_scope.pack(side="right")
        else:
            self.lbl_scope.configure(text="")
            if self.lbl_scope.winfo_manager():
                self.lbl_scope.pack_forget()
        # 手动范围被禁用时隐藏入口
        if not self.cfg.get("allow_manual_scope", True):
            if self.btn_scope.winfo_manager():
                self.btn_scope.pack_forget()
        elif not self.btn_scope.winfo_manager():
            self.btn_scope.pack(side="right", padx=(3, 0))

    def clear_scope(self) -> None:
        if not self.scope and not self.scope_follow:
            return
        self._apply_scope("", follow=False)
        self.lbl_status.configure(text="已恢复全盘搜索（自动跟随已暂停）")

    def _toggle_follow(self) -> None:
        self.scope_follow = bool(self.var_follow.get())
        if self.scope_follow and self.anchor_hwnd and shellpath is not None:
            try:
                p = shellpath.path_for_hwnd(self.anchor_hwnd)
                if p:
                    self._apply_scope(p, follow=True)
            except Exception:
                pass
        else:
            self._render_scope()
        self.lbl_status.configure(text="已开启自动跟随当前文件夹" if self.scope_follow
                                  else "已暂停自动跟随，可手动选择范围")

    def _popup_scope_menu(self, event=None) -> None:
        if not self.cfg.get("allow_manual_scope", True):
            return
        self._modal_until = time.time() + 2.0
        self.var_follow.set(self.scope_follow)
        state = "normal" if self.cfg.get("allow_manual_scope", True) else "disabled"
        for i in (2, 3, 4, 5):
            try:
                self.scope_menu.entryconfigure(i, state=state if i in (2, 3) else "normal")
            except tk.TclError:
                pass
        try:
            if event is not None:
                self.scope_menu.tk_popup(event.x_root, event.y_root)
            else:
                x = self.btn_scope.winfo_rootx()
                y = self.btn_scope.winfo_rooty() + self.btn_scope.winfo_height()
                self.scope_menu.tk_popup(x, y)
        finally:
            self.scope_menu.grab_release()

    def _choose_scope(self) -> None:
        if not self.cfg.get("allow_manual_scope", True):
            return
        self._modal_until = time.time() + 30.0
        try:
            path = filedialog.askdirectory(
                parent=self.root, title="选择 QuickIndex 的搜索范围",
                initialdir=self.scope or os.path.expanduser("~"))
        except Exception:
            path = ""
        self._modal_until = time.time() + 1.0
        if path:
            self._apply_scope(path, follow=False)
            self.lbl_status.configure(text=f"手动范围：{path}（自动跟随已暂停）")
            self.entry.focus_force()

    def _scope_parent(self) -> None:
        if not self.scope:
            return
        parent = os.path.dirname(self.scope.rstrip("\\/"))
        if parent and os.path.isdir(parent):
            self._apply_scope(parent, follow=False)

    def _copy_scope(self) -> None:
        if not self.scope:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.scope)
        self.lbl_status.configure(text="已复制范围：" + self.scope)

    def _scope_to_selected(self) -> None:
        it = self._selected_item()
        if it:
            self._apply_scope(it["dir"], follow=False)
            self.entry.focus_force()

    def _scope_watch_loop(self) -> None:
        """后台线程：资源管理器里点进某个文件夹，范围自动跟过去。"""
        if shellpath is None:
            return
        try:
            shellpath.ole32.CoInitializeEx(None, 2)  # COM 需要按线程初始化
        except Exception:
            pass
        while True:
            time.sleep(SCOPE_WATCH_S)
            try:
                if not self.visible or not self.scope_follow or not self.anchor_hwnd:
                    continue
                if not (self.cfg.get("attach_to_explorer", True)
                        and self.cfg.get("auto_scope_follow", True)):
                    continue
                p = shellpath.path_for_hwnd(self.anchor_hwnd)
                if p:
                    self.q.put(("scope_path", p))
            except Exception:
                continue

    def open_from_explorer(self, scope: str, prefill: str) -> None:
        """资源管理器右键菜单：带上目录范围 + 预填词弹出面板。"""
        scope = (scope or "").strip().strip('"').replace("/", "\\")
        if scope:
            scope = os.path.normpath(scope)
        log(f"资源管理器唤起 scope={scope!r} prefill={prefill!r}")
        # 右键菜单指定的目录属于「手动选择」：锁定，避免被自动跟随立刻覆盖；
        # 想恢复跟随可在「范围」菜单里勾选「自动跟随当前文件夹」。
        self._apply_scope(scope, refresh=False, follow=False)
        self.var_q.set(prefill or "")
        self.show(detect=False, from_explorer=True)
        # 面板已经显示时 show() 只会提到最前，这里补一次刷新保证范围/预填生效
        self.root.after(220, lambda: self.run_search())
        self.lbl_status.configure(text=f"在 {scope} 中搜索")

    # ------------------------------------------------------------ 搜索
    def _on_change(self):
        if self.debounce_id:
            try:
                self.root.after_cancel(self.debounce_id)
            except tk.TclError:
                pass
        # 长条形态下输入关键词自动展开结果（延后执行，避开变量 trace）
        if (self.visible and self.compact_bar and not self.expanded
                and self.cfg.get("expand_on_type", True)
                and self.var_q.get().strip()):
            self.root.after_idle(self._auto_expand)
        self.debounce_id = self.root.after(110, lambda: self.run_search())

    def run_search(self, initial: bool = False):
        raw = self.var_q.get().strip()
        parts = [raw] if raw else []
        t = TYPES[self.cmb_type.current()][1] if self.cmb_type.current() >= 0 else ""
        if t:
            parts.append("type:" + t)
        sort = SORTS[self.cmb_sort.current()][1] if self.cmb_sort.current() >= 0 else "relevance"
        if not raw:
            parts.insert(0, "type:file")
            sort = "mtime"
        query = " ".join(p for p in parts if p).strip()
        limit = 80 if not raw else 300
        self.gen += 1
        gen = self.gen

        def worker():
            try:
                res = self.indexer.search(query, limit=limit, sort=sort,
                                          scope=self.scope)
                self.q.put(("result", gen, res, query))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("error", gen, str(exc)))

        threading.Thread(target=worker, name="quickindex-search", daemon=True).start()

    def _render(self, res, query):
        self.items = res["items"]
        self.tree.delete(*self.tree.get_children())
        for i, it in enumerate(self.items):
            size = "文件夹" if it["is_dir"] else human_size(it["size"])
            self.tree.insert("", "end", iid=str(i),
                             values=(it["name"], size, fmt_time(it["mtime"]), it["dir"]))
        if self.items:
            self.tree.selection_set("0")
            self.tree.focus("0")
        if self.scope:
            headline = "本目录"
        else:
            headline = "最近修改" if not self.var_q.get().strip() else "搜索结果"
        self.lbl_count.configure(text=f"{headline} {res['count']}{'+' if res['has_more'] else ''} 条 · {res['elapsed_ms']:.1f} ms")
        if self.visible:
            first = self.items[0]["path"] if self.items else ""
            self.lbl_status.configure(text=first or "没有匹配的文件")

    def _move(self, delta: int):
        sel = self.tree.selection()
        if not sel:
            if self.tree.get_children():
                self.tree.selection_set("0")
            return "break"
        idx = int(sel[0]) + delta
        kids = self.tree.get_children()
        if 0 <= idx < len(kids):
            self.tree.selection_set(str(idx))
            self.tree.see(str(idx))
            self._sync_status()
        return "break"

    def _sync_status(self):
        sel = self.tree.selection()
        if sel and self.items:
            i = int(sel[0])
            if 0 <= i < len(self.items):
                self.lbl_status.configure(text=self.items[i]["path"])

    def _on_return(self, _e):
        self._open_selected()
        return "break"

    def _selected_item(self):
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return self.items[int(sel[0])]
        except (ValueError, IndexError):
            return None

    def _open_selected(self, reveal: bool = False):
        it = self._selected_item()
        if not it:
            return
        self.indexer.record_open(it["path"], it["name"])
        res = self.indexer.open_path(it["path"], reveal=reveal)
        if not res["ok"]:
            self.lbl_status.configure(text=res.get("message", "打开失败"))
        elif self.cfg.get("hide_after_open", True):
            self.hide()

    def _copy_path(self):
        it = self._selected_item()
        if not it:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(it["path"])
        self.lbl_status.configure(text="已复制：" + it["path"])

    def _context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self._sync_status()
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # ------------------------------------------------------------ 设置 / 其它动作
    def _open_settings(self):
        try:
            if self._settings_win is not None and self._settings_win.winfo_exists():
                self._settings_win.lift()
                self._settings_win.focus_force()
                return
        except Exception:
            self._settings_win = None
        try:
            import settings
            self._settings_win = settings.open_settings(self)
        except Exception as exc:  # noqa: BLE001
            log("打开设置失败：\n" + traceback.format_exc())
            self.lbl_status.configure(text="设置打开失败：" + str(exc))
            self._open_config_file()

    def apply_settings(self, patch: dict) -> None:
        """设置窗口保存后立即生效（热键 / 托盘 / 右键菜单见窗口内提示）。"""
        self.cfg = self.indexer.config
        self.compact_bar = bool(self.cfg.get("compact_bar", True))
        self.scope_follow = bool(self.cfg.get("auto_scope_follow", True))
        try:
            self.var_follow.set(self.scope_follow)
        except Exception:
            pass
        self.lbl_hotkey.configure(text=str(self.cfg.get("hotkey", "ctrl+alt+f")).upper())
        minutes = int(self.cfg.get("scan_interval_minutes", 15))
        self.next_scan = time.time() + max(1, minutes) * 60
        self._render_scope()
        if self.visible:
            want = (not self.compact_bar) or (
                bool(self.cfg.get("expand_on_type", True)) and bool(self.var_q.get().strip()))
            if want != self.expanded:
                self._set_expanded(want)
            else:
                self._apply_view(keep_pos=True)
            if not self.cfg.get("attach_to_explorer", True):
                self.anchor_hwnd = 0
                self.anchor_rect = None
            elif not self.anchor_hwnd:
                hwnd, rect = self._pick_anchor(self.scope)
                if hwnd and rect:
                    self._reanchor(hwnd, rect)
        if autostart is not None:
            try:
                autostart.set_autostart(bool(self.cfg.get("enable_autostart", True)))
            except Exception:
                pass
        if explorer_ext is not None:
            try:
                explorer_ext.set_menu(bool(self.cfg.get("enable_explorer_menu", True)))
            except Exception:
                pass
        log("设置已保存：" + ",".join(f"{k}={v}" for k, v in patch.items()
                                    if isinstance(v, (bool, int, str))))

    def restart_daemon(self) -> None:
        """用独立进程重启常驻，让热键 / 托盘开关生效。"""
        try:
            py = sys.executable
            # pythonw.exe 没有 stdout，autostart.py 里的 print 会报 lost sys.stdout；
            # 换成同目录的 python.exe 并 CREATE_NO_WINDOW，既安全又不闪控制台
            if os.path.basename(py).lower() == "pythonw.exe":
                cand = os.path.join(os.path.dirname(py), "python.exe")
                if os.path.isfile(cand):
                    py = cand
            subprocess.Popen(
                [py, os.path.join(APP_DIR, "autostart.py"), "--restart"],
                cwd=APP_DIR, creationflags=0x08000000 | 0x00000008,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True)
        except Exception:
            log("重启常驻失败：\n" + traceback.format_exc())

    def _open_config_file(self):
        path = self.indexer.config_path
        if not os.path.exists(path):
            self.indexer.save_config({})
        try:
            os.startfile(path)  # noqa: S606
        except OSError as exc:
            self.lbl_status.configure(text=str(exc))

    def _scan(self, deep: bool):
        res = self.indexer.start_index(deep=deep)
        self.lbl_status.configure(text=res["message"])
        if not res["ok"]:
            return
        self.next_scan = time.time() + max(1, int(self.cfg.get("scan_interval_minutes", 15))) * 60

    def _shutdown(self):
        self._save_panel_size()
        try:
            os.remove(PID_PATH)
        except OSError:
            pass
        log("退出")
        self.root.destroy()
        os._exit(0)

    # ------------------------------------------------------------ 主循环泵
    def _tick(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "result":
                    _, gen, res, query = msg
                    if gen == self.gen:
                        self._render(res, query)
                elif kind == "error":
                    self.lbl_status.configure(text="搜索失败：" + str(msg[2]))
                elif kind == "scope_path":
                    path = str(msg[1])
                    if (self.visible and self.scope_follow
                            and self.cfg.get("auto_scope_follow", True)
                            and self.cfg.get("attach_to_explorer", True)
                            and normcase(path) != normcase(self.scope)):
                        self._apply_scope(path)
                        self.lbl_status.configure(text=f"自动范围：{path}")
                        log(f"范围自动跟随 -> {path}")
                elif kind in ("win", "ipc"):
                    payload = str(msg[1])
                    cmd, _sep, arg = payload.partition("\t")
                    cmd = cmd.upper()          # 托盘/热键发的是小写
                    if cmd == "SHOW":
                        self.show()
                    elif cmd == "OPEN":
                        parts = (arg.split("\t") + ["", ""])[:2]
                        self.open_from_explorer(parts[0], parts[1])
                    elif cmd == "HIDE":
                        self.hide()
                    elif cmd == "SETTINGS":
                        self._open_settings()
                    elif cmd == "SCAN":
                        self._scan(False)
                    elif cmd == "DEEP":
                        self._scan(True)
                    elif cmd == "CONFIG":
                        self._open_settings()
                    elif cmd == "QUIT":
                        self.root.after(0, self._shutdown)
                elif kind == "select":
                    self._sync_status()
        except queue.Empty:
            pass

        st = self.indexer.status
        if st.get("indexing"):
            txt = f"{st.get('message', '扫描中')} · 已检查 {st.get('scanned', 0):,} 项"
            if txt != self._last_status_text:
                self._last_status_text = txt
                self.lbl_status.configure(text=txt)
        minutes = int(self.cfg.get("scan_interval_minutes", 15))
        if minutes > 0 and time.time() >= self.next_scan and not st.get("indexing"):
            self.indexer.start_index()
            self.next_scan = time.time() + minutes * 60
        self.root.after(120, self._tick)


if __name__ == "__main__":
    try:
        QuickIndexApp()
    except SystemExit:
        raise
    except Exception:
        log("启动失败：\n" + traceback.format_exc())
        raise
