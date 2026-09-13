import argparse

import query
from diffords_guide.storage import DiffordsStorage
from tests.unit.test_diffords import _sample_cocktail


def test_query_search_outputs_results(tmp_path, capsys):
    db_path = tmp_path / "diffords.db"
    with DiffordsStorage(str(db_path)) as storage:
        storage.save_cocktail(_sample_cocktail())

    args = argparse.Namespace(db=str(db_path), keyword="neg", limit=10, sort="rating", asc=False)
    query.cmd_search(args)

    out = capsys.readouterr().out
    assert "Negroni" in out


def test_query_list_filters_by_ingredient(tmp_path, capsys):
    db_path = tmp_path / "diffords.db"
    with DiffordsStorage(str(db_path)) as storage:
        storage.save_cocktail(_sample_cocktail())

    args = argparse.Namespace(
        db=str(db_path),
        keyword=None,
        description=None,
        ingredient="Campari",
        tag=None,
        rating=None,
        max_rating=None,
        abv=None,
        max_abv=None,
        min_count=None,
        limit=10,
        sort="rating",
        asc=False,
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
        for cid, name, rating, abv in [
            (1, "Low Gin", 4.5, 10.0),
            (2, "High Gin", 4.5, 40.0),
            (3, "Bad Gin", 2.0, 50.0),
        ]:
            # url 決定 id；ingredients_html 缺 sort_order 會讓 save 靜默失敗
            assert st.save_cocktail({
                "name": name,
                "url": f"https://www.diffordsguide.com/cocktails/recipe/{cid}/x",
                "rating_value": rating, "rating_count": 20, "abv": abv,
                "ingredients_html": [{"sort_order": 0, "item": "Gin", "amount": "30ml"}],
            }) is True

    parser = query.build_parser()
    args = parser.parse_args([
        "--db", str(db), "list",
        "--ingredient", "gin", "--rating", "4.0", "--sort", "abv",
    ])
    args.func(args)

    out = capsys.readouterr().out
    assert "Bad Gin" not in out                          # 被 --rating 4.0 濾掉
    assert out.index("High Gin") < out.index("Low Gin")  # --sort abv 降序


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
    assert out.index("Weak") < out.index("Strong")


def test_invalid_sort_key_rejected_by_argparse():
    import pytest as _pytest

    parser = query.build_parser()
    with _pytest.raises(SystemExit):
        parser.parse_args(["list", "--sort", "nope"])
