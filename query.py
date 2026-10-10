#!/usr/bin/env python3
"""Difford's Guide cocktail database query CLI."""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from diffords_guide.config import DIFFORDS_DB_DEFAULT


def _truncate(text: str | None, width: int) -> str:
    if not text:
        return ""
    return text[: width - 1] + "…" if len(text) > width else text


def _positive_int(value: str) -> int:
    """--limit 的驗證：SQLite 把負數 LIMIT 當成無上限，會靜默印出全部 6955 筆。"""
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"必須是正整數，收到 {value}")
    return number


def cmd_ask(args: argparse.Namespace) -> None:
    """本機版 LINE bot：搜尋／酒譜／列表／統計都走 bot 的同一套指令。"""
    import bot

    print(bot.handle_message(args.text, args.db))


def cmd_index(args: argparse.Namespace) -> None:
    """建立／更新風味語意檢索的向量索引。"""
    from diffords_guide import embeddings

    if not Path(args.db).exists():
        print(f"資料庫不存在：{args.db}")
        raise SystemExit(1)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    stats = embeddings.build_index(args.db, rebuild=args.rebuild)
    print("\n向量索引完成")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    if stats["失敗"]:
        raise SystemExit(1)


def cmd_similar(args: argparse.Namespace) -> None:
    """用風味描述做語意檢索（需要先跑 index）。"""
    from diffords_guide import embeddings

    results = embeddings.search(args.db, args.query, limit=args.limit)
    if results is None:
        print("語意檢索無法使用：請確認 GEMINI_API_KEY 已設定且已執行 `query.py index`。")
        raise SystemExit(1)
    if not results:
        print("（無結果）")
        return
    print(f'\n喝起來像「{args.query}」：\n')
    for i, row in enumerate(results, 1):
        print(f"{i}. {row['name']}  (相似度 {row['score']:.3f})")
        if row.get("review"):
            print(f"   {_truncate(row['review'], 78)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Difford's Guide 雞尾酒資料庫查詢工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""範例:
  uv run python query.py ask 雞尾酒統計
  uv run python query.py ask "雞尾酒搜尋 negroni"
  uv run python query.py ask "雞尾酒酒譜 Negroni"
  uv run python query.py ask "雞尾酒列表 材料 gin 評分 4.2 排序 酒精濃度 15筆"
  uv run python query.py similar "smoky and bitter"
""",
    )
    parser.add_argument("--db", default=DIFFORDS_DB_DEFAULT, help="SQLite DB 路徑")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ask = sub.add_parser("ask", help="用 LINE bot 的指令查詢（輸入「說明」看全部）")
    p_ask.add_argument("text")
    p_ask.set_defaults(func=cmd_ask)

    p_index = sub.add_parser("index", help="建立／更新風味語意檢索索引")
    p_index.add_argument("--rebuild", action="store_true", help="忽略既有指紋全部重算")
    p_index.set_defaults(func=cmd_index)

    p_similar = sub.add_parser("similar", help="用風味描述搜尋（需先跑 index）")
    p_similar.add_argument("query")
    p_similar.add_argument("--limit", type=_positive_int, default=10)
    p_similar.set_defaults(func=cmd_similar)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
