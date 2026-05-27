from unittest.mock import MagicMock, patch

import run_diffords


def test_run_records_scrape_run(tmp_path):
    db_path = tmp_path / "diffords.db"

    with patch("run_diffords.DiffordsGuideScraper") as scraper_cls:
        scraper = MagicMock()
        scraper.stats.scraped = 1
        scraper.stats.skipped = 0
        scraper.stats.failed = 0
        scraper.scrape.return_value = True
        scraper.get_statistics.return_value = {"爬取新增": 1}
        scraper_cls.return_value = scraper

        success, stats = run_diffords.run("test", str(db_path), False, MagicMock())

    assert success is True
    assert stats["爬取新增"] == 1


def test_do_notify_success_calls_line_notifier():
    notifier = MagicMock()
    notifier.notify_success.return_value = True

    ok = run_diffords._do_notify(notifier, True, "test", {"爬取新增": 2}, None, 5)

    assert ok is True
    notifier.notify_success.assert_called_once()
