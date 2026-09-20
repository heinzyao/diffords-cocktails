
import query
from diffords_guide.storage import DiffordsStorage
from tests.unit.test_diffords import _sample_cocktail


def test_query_search_outputs_results(tmp_path, capsys):
    db_path = tmp_path / "diffords.db"
    with DiffordsStorage(str(db_path)) as storage:
        storage.save_cocktail(_sample_cocktail())

    # 用真正的 parser 建 args：手工列舉 Namespace 欄位的話，
    # 每次新增 CLI 選項都會讓這個測試壞掉，而且測不到 parser 本身。
    args = query.build_parser().parse_args(
        ["--db", str(db_path), "search", "neg", "--limit", "10"]
    )
    query.cmd_search(args)

    out = capsys.readouterr().out
    assert "Negroni" in out


def test_query_list_filters_by_ingredient(tmp_path, capsys):
    db_path = tmp_path / "diffords.db"
    with DiffordsStorage(str(db_path)) as storage:
        storage.save_cocktail(_sample_cocktail())

    args = query.build_parser().parse_args(
        ["--db", str(db_path), "list", "--ingredient", "Campari", "--limit", "10"]
    )
    query.cmd_list(args)

    out = capsys.readouterr().out
    assert "Campari" in out
    assert "Negroni" in out


def test_query_parser_has_no_spirit_commands():
    parser = query.build_parser()
    args = parser.parse_args(["search", "negroni"])
    assert args.command == "search"
    assert "spirits" not in parser.format_help()


def test_list_flags_combine_and_sort(capsys, tmp_path):
    """--ingredient 與 --rating 疊加，且 --sort abv 生效。"""
    db = tmp_path / "t.db"
    with DiffordsStorage(str(db)) as st:
        for cid, name, rating, abv, ingredients in [
            (1, "Low Gin", 4.5, 10.0, [{"sort_order": 0, "item": "Gin", "amount": "30ml"}]),
            (2, "High Gin", 4.5, 40.0, [{"sort_order": 0, "item": "Gin", "amount": "30ml"}]),
            (3, "Bad Gin", 2.0, 50.0, [{"sort_order": 0, "item": "Gin", "amount": "30ml"}]),
            (4, "Good Rum", 4.5, 20.0, [{"sort_order": 0, "item": "Rum", "amount": "30ml"}]),
        ]:
            # url 決定 id；ingredients_html 缺 sort_order 會讓 save 靜默失敗
            assert st.save_cocktail({
                "name": name,
                "url": f"https://www.diffordsguide.com/cocktails/recipe/{cid}/x",
                "rating_value": rating, "rating_count": 20, "abv": abv,
                "ingredients_html": ingredients,
            }) is True

    parser = query.build_parser()
    args = parser.parse_args([
        "--db", str(db), "list",
        "--ingredient", "gin", "--rating", "4.0", "--sort", "abv",
    ])
    args.func(args)

    out = capsys.readouterr().out
    # 預期結果：只有 Low Gin 和 High Gin 符合「含 Gin AND 評分 >= 4.0」
    # Good Rum 被 --ingredient gin 濾掉；Bad Gin 被 --rating 4.0 濾掉
    expected = ["Low Gin", "High Gin"]
    output_lines = [line for line in out.split('\n') if any(name in line for name in expected)]
    assert len(output_lines) == len(expected), f"Expected {expected} in output, got: {output_lines}"

    # 驗證 --sort abv 降序生效：High Gin (40.0) 應在 Low Gin (10.0) 之前
    assert "High Gin" in out and "Low Gin" in out, "High Gin and Low Gin must both be in output"
    assert out.index("High Gin") < out.index("Low Gin")  # --sort abv 降序
    assert "Good Rum" not in out  # 被 --ingredient gin 濾掉
    assert "Bad Gin" not in out   # 被 --rating 4.0 濾掉


def test_list_asc_flag(capsys, tmp_path):
    db = tmp_path / "t.db"
    with DiffordsStorage(str(db)) as st:
        for cid, name, abv in [(1, "Weak", 5.0), (2, "Strong", 50.0)]:
            assert st.save_cocktail({
                "name": name,
                "url": f"https://www.diffordsguide.com/cocktails/recipe/{cid}/x",
                "rating_value": 4.0, "rating_count": 10, "abv": abv,
                "ingredients_html": [],
            }) is True

    parser = query.build_parser()
    args = parser.parse_args(["--db", str(db), "list", "--sort", "abv", "--asc"])
    args.func(args)

    out = capsys.readouterr().out
    assert "Weak" in out and "Strong" in out, "Both Weak and Strong must be in output"
    assert out.index("Weak") < out.index("Strong")


def test_invalid_sort_key_rejected_by_argparse():
    import pytest as _pytest

    parser = query.build_parser()
    with _pytest.raises(SystemExit):
        parser.parse_args(["list", "--sort", "nope"])


def test_list_applies_min_count_without_filters(capsys, tmp_path):
    """--sort 或 --limit 無篩選時，仍應套用 min_count=5 門檻。"""
    db = tmp_path / "t.db"
    with DiffordsStorage(str(db)) as st:
        # 評分達標但評分人數不足
        assert st.save_cocktail({
            "name": "Unpopular",
            "url": "https://www.diffordsguide.com/cocktails/recipe/1/x",
            "rating_value": 4.5, "rating_count": 3,  # < 5
            "abv": 10.0,
            "ingredients_html": [{"sort_order": 0, "item": "Vodka", "amount": "30ml"}],
        }) is True
        # 評分達標且評分人數足夠
        assert st.save_cocktail({
            "name": "Popular",
            "url": "https://www.diffordsguide.com/cocktails/recipe/2/x",
            "rating_value": 4.5, "rating_count": 20,  # >= 5
            "abv": 20.0,
            "ingredients_html": [{"sort_order": 0, "item": "Vodka", "amount": "30ml"}],
        }) is True

    parser = query.build_parser()
    args = parser.parse_args([
        "--db", str(db), "list",
        "--sort", "abv",  # 只有排序，無篩選條件
    ])
    args.func(args)

    out = capsys.readouterr().out
    assert "Popular" in out, "Popular (rating_count >= 5) should be in output"
    assert "Unpopular" not in out, "Unpopular (rating_count < 5) should be filtered out by default min_count=5"


def test_limit_rejects_non_positive_values():
    """SQLite 把負數 LIMIT 當成無上限：--limit -1 會靜默印出全部 6955 筆。"""
    import pytest as _pytest

    import query

    parser = query.build_parser()
    for bad in ("-1", "0"):
        for sub in ("list", "search"):
            argv = [sub, "--limit", bad] if sub == "list" else [sub, "kw", "--limit", bad]
            with _pytest.raises(SystemExit):
                parser.parse_args(argv)

    # 正常值仍可用
    assert parser.parse_args(["list", "--limit", "15"]).limit == 15
