#!/usr/bin/env python3
"""Difford's Guide cocktail database query CLI."""

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from diffords_guide.config import DIFFORDS_DB_DEFAULT
from diffords_guide.storage import DiffordsStorage


def _truncate(text: str | None, width: int) -> str:
    if not text:
        return ""
    return text[: width - 1] + "…" if len(text) > width else text


def _print_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("（無結果）")
        return
    for idx, cocktail in enumerate(rows, 1):
        rating = cocktail.get("rating_value")
        rating_text = f"{rating:.1f}" if isinstance(rating, (int, float)) else "N/A"
        desc = _truncate(cocktail.get("description"), 80)
        print(f"{idx}. {cocktail['name']} ({rating_text})")
        if desc:
            print(f"   {desc}")


def _open_storage(db_path: str) -> DiffordsStorage:
    if not Path(db_path).exists():
        print(f"資料庫不存在：{db_path}")
        print("請先執行：uv run python run_diffords.py --mode test")
        raise SystemExit(1)
    return DiffordsStorage(db_path)


def cmd_search(args: argparse.Namespace) -> None:
    with _open_storage(args.db) as storage:
        rows = storage.search_cocktails(args.keyword, limit=args.limit)
    print(f'\n搜尋 "{args.keyword}"：找到 {len(rows)} 筆\n')
    _print_rows(rows)


def cmd_info(args: argparse.Namespace) -> None:
    with _open_storage(args.db) as storage:
        cocktail = storage.get_cocktail_by_name(args.name)
    if not cocktail:
        print(f'找不到符合 "{args.name}" 的雞尾酒。')
        return

    print(f"\n{'=' * 60}")
    print(f"  {cocktail['name']}")
    print(f"{'=' * 60}")

    fields = [
        ("評分", "rating_value"),
        ("評分數", "rating_count"),
        ("ABV", "abv"),
        ("杯型", "glassware"),
        ("裝飾", "garnish"),
        ("準備方式", "prepare"),
        ("卡路里", "calories"),
        ("發布日期", "date_published"),
    ]
    for label, key in fields:
        value = cocktail.get(key)
        if value not in (None, ""):
            print(f"  {label}: {value}")

    ingredients = cocktail.get("ingredients") or []
    if ingredients:
        print("\n  食材:")
        for ingredient in ingredients:
            amount = ingredient.get("amount") or ""
            item = ingredient.get("item") or ""
            print(f"    - {amount} {item}".strip())

    for label, key, width in [
        ("作法", "instructions", 800),
        ("評語", "review", 500),
        ("歷史", "history", 500),
        ("描述", "description", 500),
    ]:
        value = _truncate(cocktail.get(key), width)
        if value:
            print(f"\n  {label}:\n  {value}")

    if cocktail.get("url"):
        print(f"\n  URL: {cocktail['url']}")
    print()


def cmd_stats(args: argparse.Namespace) -> None:
    with _open_storage(args.db) as storage:
        stats = storage.get_stats()
    print(f"\nDifford's Guide 資料庫統計（{args.db}）")
    print("-" * 40)
    for key, value in stats.items():
        print(f"  {key}: {value}")
    print()


def cmd_list(args: argparse.Namespace) -> None:
    with _open_storage(args.db) as storage:
        if args.ingredient:
            rows = storage.filter_by_ingredient(args.ingredient, limit=args.limit)
            title = f"含「{args.ingredient}」"
        elif args.tag:
            rows = storage.filter_by_tag(args.tag, limit=args.limit)
            title = f"標籤「{args.tag}」"
        elif args.rating is not None:
            rows = storage.filter_by_rating(min_rating=args.rating, limit=args.limit)
            title = f"評分 >= {args.rating}"
        elif args.abv is not None:
            rows = storage.filter_by_abv(min_abv=args.abv, limit=args.limit)
            title = f"ABV >= {args.abv}"
        else:
            rows = storage.get_top_rated(limit=args.limit)
            title = "評分排序"
    print(f"\n雞尾酒列表（{title}，顯示 {len(rows)} 筆）\n")
    _print_rows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Difford's Guide 雞尾酒資料庫查詢工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""範例:
  uv run python query.py stats
  uv run python query.py search negroni
  uv run python query.py info "Negroni"
  uv run python query.py list --ingredient gin --limit 10
  uv run python query.py list --tag Classic/vintage
  uv run python query.py list --rating 4.5
""",
    )
    parser.add_argument("--db", default=DIFFORDS_DB_DEFAULT, help="SQLite DB 路徑")
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="搜尋雞尾酒名稱")
    p_search.add_argument("keyword")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.set_defaults(func=cmd_search)

    p_info = sub.add_parser("info", help="顯示完整酒譜")
    p_info.add_argument("name")
    p_info.set_defaults(func=cmd_info)

    p_stats = sub.add_parser("stats", help="顯示資料庫統計")
    p_stats.set_defaults(func=cmd_stats)

    p_list = sub.add_parser("list", help="列出或篩選雞尾酒")
    p_list.add_argument("--ingredient", help="依食材篩選")
    p_list.add_argument("--tag", help="依標籤篩選")
    p_list.add_argument("--rating", type=float, help="最低評分")
    p_list.add_argument("--abv", type=float, help="最低 ABV")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.set_defaults(func=cmd_list)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
