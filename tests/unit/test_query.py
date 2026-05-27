import argparse

import query
from diffords_guide.storage import DiffordsStorage
from tests.unit.test_diffords import _sample_cocktail


def test_query_search_outputs_results(tmp_path, capsys):
    db_path = tmp_path / "diffords.db"
    with DiffordsStorage(str(db_path)) as storage:
        storage.save_cocktail(_sample_cocktail())

    args = argparse.Namespace(db=str(db_path), keyword="neg", limit=10)
    query.cmd_search(args)

    out = capsys.readouterr().out
    assert "Negroni" in out


def test_query_list_filters_by_ingredient(tmp_path, capsys):
    db_path = tmp_path / "diffords.db"
    with DiffordsStorage(str(db_path)) as storage:
        storage.save_cocktail(_sample_cocktail())

    args = argparse.Namespace(
        db=str(db_path),
        ingredient="Campari",
        tag=None,
        rating=None,
        abv=None,
        limit=10,
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
