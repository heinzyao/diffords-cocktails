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


def _main_with(monkeypatch, argv, index_stats=None, index_exc=None):
    """跑一次 main()，把爬蟲與 GCS 都換成 mock，回傳 (上傳是否發生, build_index mock)。"""
    monkeypatch.setattr("sys.argv", ["run_diffords.py", *argv])
    monkeypatch.setenv("GCS_BUCKET", "test-bucket")

    build_index = MagicMock()
    if index_exc is not None:
        build_index.side_effect = index_exc
    else:
        build_index.return_value = index_stats or {"總數": 3, "已寫入": 3, "失敗": 0}

    # gcs_storage 是 submodule，run_diffords 在函式內才 import 它，
    # 所以 patch 個別函式（會自動載入模組），不要 patch 模組物件本身。
    with patch("run_diffords.run", return_value=(True, {"爬取新增": 1})), \
         patch("diffords_guide.gcs_storage.download_db", return_value=True), \
         patch("diffords_guide.gcs_storage.upload_db", return_value=True) as upload, \
         patch("diffords_guide.embeddings.build_index", build_index):
        try:
            run_diffords.main()
        except SystemExit as exc:
            assert exc.code == 1  # 失敗路徑會 exit(1)
    return upload.called, build_index


def test_build_index_runs_before_gcs_upload(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    uploaded, build_index = _main_with(monkeypatch, ["--build-index"])

    build_index.assert_called_once()
    assert uploaded is True


def test_index_failure_blocks_gcs_upload(monkeypatch):
    """索引沒建完就上傳，線上會有查不到風味的新酒譜 —— 寧可保留舊 DB。"""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    uploaded, _ = _main_with(
        monkeypatch, ["--build-index"], index_stats={"總數": 3, "已寫入": 1, "失敗": 2}
    )

    assert uploaded is False


def test_index_exception_blocks_gcs_upload(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    uploaded, _ = _main_with(
        monkeypatch, ["--build-index"], index_exc=RuntimeError("boom")
    )

    assert uploaded is False


def test_missing_api_key_skips_index_without_blocking_upload(monkeypatch):
    """沒金鑰是正常狀態（與其他 LLM 功能同契約），不該擋下整次排程。"""
    monkeypatch.setenv("GEMINI_API_KEY", "")
    uploaded, build_index = _main_with(monkeypatch, ["--build-index"])

    build_index.assert_not_called()
    assert uploaded is True


def test_index_skipped_entirely_without_flag(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    uploaded, build_index = _main_with(monkeypatch, [])

    build_index.assert_not_called()
    assert uploaded is True
