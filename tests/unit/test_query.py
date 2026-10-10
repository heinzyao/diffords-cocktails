import pytest

import query
from diffords_guide.storage import DiffordsStorage
from tests.unit.test_diffords import _sample_cocktail


def test_ask_routes_through_bot_commands(tmp_path, capsys):
    db_path = tmp_path / "diffords.db"
    with DiffordsStorage(str(db_path)) as storage:
        storage.save_cocktail(_sample_cocktail())

    args = query.build_parser().parse_args(["--db", str(db_path), "ask", "雞尾酒搜尋 neg"])
    args.func(args)

    assert "Negroni" in capsys.readouterr().out


def test_limit_rejects_non_positive_values():
    """SQLite 把負數 LIMIT 當成無上限：--limit -1 會靜默印出全部 6955 筆。"""
    parser = query.build_parser()
    for bad in ("-1", "0"):
        with pytest.raises(SystemExit):
            parser.parse_args(["similar", "kw", "--limit", bad])

    assert parser.parse_args(["similar", "kw", "--limit", "15"]).limit == 15
