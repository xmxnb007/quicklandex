# -*- coding: utf-8 -*-
"""QuickIndex 命令行工具

    python cli.py scan              增量扫描
    python cli.py scan --rebuild    全量重建
    python cli.py find 报告          搜索
    python cli.py find 大图 type:image size:>5MB --limit 20
    python cli.py stats             查看索引统计
    python cli.py serve             启动图形界面
    python cli.py roots D:\\        设置索引范围（多个用逗号分隔）
"""

from __future__ import annotations

import sys
import time

from engine import Indexer, human_size


def cmd_scan(idx: Indexer, args: list[str]) -> None:
    rebuild = "--rebuild" in args
    roots = None
    for i, a in enumerate(args):
        if a == "--roots" and i + 1 < len(args):
            roots = args[i + 1].split(",")
    idx.start_index(rebuild=rebuild, roots=roots)
    last = ""
    while idx.status["indexing"]:
        msg = f"  扫描 {idx.status['scanned']:,} · 写入 {idx.status['written']:,}"
        sys.stdout.write("\r" + msg + " " * 12)
        sys.stdout.flush()
        time.sleep(0.3)
    print("\r" + idx.status["message"] + " " * 20)


def cmd_find(idx: Indexer, args: list[str]) -> None:
    limit = 30
    sort = "relevance"
    words = []
    i = 0
    while i < len(args):
        if args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
            i += 2
            continue
        if args[i] == "--sort" and i + 1 < len(args):
            sort = args[i + 1]
            i += 2
            continue
        words.append(args[i])
        i += 1
    res = idx.search(" ".join(words), limit=limit, sort=sort)
    print(f"  {res['count']} 个结果 · {res['elapsed_ms']} ms" + ("（还有更多）" if res["has_more"] else ""))
    for it in res["items"]:
        size = "   <DIR>" if it["is_dir"] else human_size(it["size"]).rjust(9)
        print(f"  {size}  {it['name']}")
        print(f"           {it['dir']}")


def cmd_stats(idx: Indexer) -> None:
    s = idx.stats()
    print(f"  数据库      {idx.db_path}")
    print(f"  索引范围    {', '.join(s['roots'])}")
    print(f"  条目总数    {s['total']:,}（文件 {s['files']:,} / 目录 {s['dirs']:,}）")
    print(f"  总体积      {human_size(s['bytes'])}")
    print(f"  今天改动    {s['modified_today']:,}")
    print(f"  索引占用    {human_size(s['db_size'])}")
    if s["last_index_at"]:
        print(f"  上次索引    {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(s['last_index_at']))}")
    if s["top_ext"]:
        print("  常见类型    " + "  ".join(f".{x['ext']}×{x['count']}" for x in s["top_ext"]))


def cmd_roots(idx: Indexer, args: list[str]) -> None:
    if not args:
        print("  当前范围：")
        for r in idx.config["roots"]:
            print("   -", r)
        return
    roots = [r.strip() for r in args[0].split(",") if r.strip()]
    idx.save_config({"roots": roots})
    print("  已设置索引范围：", ", ".join(roots))
    print("  执行 python cli.py scan --rebuild 生效")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd, args = sys.argv[1], sys.argv[2:]
    idx = Indexer()
    if cmd == "scan":
        cmd_scan(idx, args)
    elif cmd == "find":
        cmd_find(idx, args)
    elif cmd == "stats":
        cmd_stats(idx)
    elif cmd == "roots":
        cmd_roots(idx, args)
    elif cmd == "serve":
        import app
        app.main()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
