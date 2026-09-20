#!/usr/bin/env python3
"""
Difford's Guide 雞尾酒譜爬蟲執行腳本

用法:
    python run_diffords.py --mode incremental    # 增量更新（預設，排程使用）
    python run_diffords.py --mode full           # 全量爬取（首次或強制重爬）
    python run_diffords.py --mode test           # 測試（僅爬 10 筆，驗證 selector）
    python run_diffords.py --notify-line         # 完成後透過 LINE 推播通知
    python run_diffords.py --build-index         # 爬完順便重建風味向量索引

執行流程：
    1. GCS 下載 diffords.db（Cloud Run 環境）
    2. 執行視窗保護：7 天內已成功執行則跳過
    3. 解析 sitemap → 決定待爬 URL
    4. 爬取雞尾酒詳情頁
    5. 重建風味向量索引（--build-index，需 GEMINI_API_KEY）
    6. GCS 上傳 diffords.db
    7. LINE 通知（成功/失敗/跳過）
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from diffords_guide.config import DIFFORDS_NOTIFY_SOURCE
from diffords_guide.notify import LineNotifier
from diffords_guide.scraper import DiffordsGuideScraper
from diffords_guide.storage import DiffordsStorage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("diffords_scraper.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def _do_notify(
    notifier: LineNotifier,
    success: bool,
    mode: str,
    stats: dict,
    exc,
    duration_secs: int,
) -> bool:
    if success:
        clean_stats = {k: v for k, v in stats.items() if not k.startswith("_")}
        return notifier.notify_success(
            mode,
            clean_stats,
            duration_secs=duration_secs,
            source=DIFFORDS_NOTIFY_SOURCE,
        )
    return notifier.notify_failure(
        mode,
        error=str(exc) if exc else "未知錯誤",
        duration_secs=duration_secs,
        source=DIFFORDS_NOTIFY_SOURCE,
    )


def run(mode: str, db_path: str, notify_line: bool, args) -> tuple[bool, dict]:
    """核心執行流程，回傳 (success, stats)。"""
    incremental = mode != "full"
    max_recipes = 10 if mode == "test" else None

    storage = DiffordsStorage(db_path)
    run_id = storage.record_scrape_run(mode)
    scraper = DiffordsGuideScraper(storage=storage)
    status = "completed"
    exc = None
    success = False

    try:
        success = scraper.scrape(
            max_recipes=max_recipes,
            incremental=incremental,
        )
        if scraper.stats.failed > 0:
            status = "completed_with_errors"
    except Exception as e:
        exc = e
        status = "failed"
        logger.exception("爬蟲執行發生例外")
    finally:
        storage.finish_scrape_run(
            run_id,
            scraped=scraper.stats.scraped,
            skipped=scraper.stats.skipped,
            failed=scraper.stats.failed,
            status=status,
        )
        scraper.close()
        storage.close()

    if exc:
        raise exc

    stats = scraper.get_statistics()
    return success, stats


def main():
    parser = argparse.ArgumentParser(description="Difford's Guide 雞尾酒譜爬蟲")
    parser.add_argument(
        "--mode",
        choices=["incremental", "full", "test"],
        default="incremental",
        help="爬取模式: incremental（增量，預設）/ full（全量）/ test（10 筆）",
    )
    parser.add_argument(
        "--db-path",
        default="diffords.db",
        help="SQLite 資料庫路徑（預設: diffords.db）",
    )
    parser.add_argument(
        "--notify-line",
        action="store_true",
        help="完成後透過 LINE Messaging API 發送通知",
    )
    parser.add_argument(
        "--build-index",
        action="store_true",
        help="爬完後重建風味向量索引（需 GEMINI_API_KEY，在 GCS 上傳前執行）",
    )
    args = parser.parse_args()

    print(f"\n開始時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"模式: {args.mode}  DB: {args.db_path}")
    _run_start = time.time()

    # ── GCS 下載（Cloud Run 環境，設定了 GCS_BUCKET 才啟用）────────────
    gcs_bucket = os.getenv("GCS_BUCKET", "")
    gcs_db_blob = os.getenv("GCS_DB_BLOB", "diffords.db")
    if gcs_bucket:
        from diffords_guide import gcs_storage

        print(f"☁️  從 GCS 下載 DB ({gcs_bucket}/{gcs_db_blob})…")
        try:
            gcs_storage.download_db(gcs_bucket, gcs_db_blob, args.db_path)
        except Exception as e:
            logger.error("GCS 下載失敗，中止執行以避免覆蓋線上資料庫：%s", e)
            sys.exit(1)

    # ── 執行爬蟲 ─────────────────────────────────────────────────────
    _exc: Exception | None = None
    try:
        success, stats = run(args.mode, args.db_path, args.notify_line, args)
    except Exception as e:
        _exc = e
        success, stats = False, {}

    duration_secs = int(time.time() - _run_start)
    print(f"結束時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # ── 重建風味向量索引 ──────────────────────────────────────────────
    # 放在 GCS 上傳之前，這樣新爬到的酒譜連同索引一次寫回，不必上傳兩次。
    # 沒設 GEMINI_API_KEY 就靜默跳過，與其他 LLM 功能同一套契約。
    if args.build_index and success and _exc is None:
        from diffords_guide import embeddings

        if not os.getenv("GEMINI_API_KEY"):
            print("\n🔎 未設定 GEMINI_API_KEY，略過向量索引重建")
        else:
            print("\n🔎 重建風味向量索引…")
            try:
                index_stats = embeddings.build_index(args.db_path)
                print(
                    f"   共 {index_stats['總數']} 筆，"
                    f"更新 {index_stats['已寫入']} 筆，失敗 {index_stats['失敗']} 筆"
                )
                # 索引沒建完就上傳，線上會拿到新酒譜但查不到它們的風味 ——
                # 寧可留著舊 DB，下次排程再補。
                if index_stats["失敗"]:
                    logger.error("向量索引有 %d 筆失敗", index_stats["失敗"])
                    success = False
            except Exception as exc:
                logger.error("向量索引重建失敗：%s", exc)
                success = False

    # ── GCS 上傳 ─────────────────────────────────────────────────────
    # 只有爬蟲成功時才回寫線上 DB，避免失敗或半更新狀態覆蓋 GCS 版本。
    if gcs_bucket and success and _exc is None:
        from diffords_guide import gcs_storage

        print(f"\n☁️  上傳 DB 至 GCS ({gcs_bucket}/{gcs_db_blob})…")
        if not gcs_storage.upload_db(gcs_bucket, gcs_db_blob, args.db_path):
            logger.error("GCS 上傳失敗")
            success = False
    elif gcs_bucket:
        logger.warning("爬蟲未成功完成，略過 GCS 上傳以保留線上 DB")

    # ── LINE 通知 ─────────────────────────────────────────────────────
    if args.notify_line:
        notifier = LineNotifier()
        if not notifier.is_configured():
            print("⚠️  LINE 通知未設定（缺少憑證環境變數）")
        else:
            def _notify():
                return _do_notify(
                    notifier,
                    success,
                    args.mode,
                    stats,
                    _exc,
                    duration_secs,
                )

            ok = _notify()
            if not ok:
                print("⚠️  LINE 通知第一次失敗，30 秒後重試…")
                time.sleep(30)
                ok = _notify()
            label = "成功通知" if success else "失敗通知"
            print(f"📱 LINE {label}{'已發送' if ok else '發送失敗'}")

    # ── 結果 ─────────────────────────────────────────────────────────
    if _exc is not None:
        raise _exc

    if success:
        scraped = stats.get("爬取新增", 0)
        skipped_count = stats.get("跳過（已是最新）", 0)
        print(f"\n✅ 執行成功！新增 {scraped} 筆，跳過 {skipped_count} 筆（已是最新）")
    else:
        print("\n❌ 執行失敗")
        sys.exit(1)


if __name__ == "__main__":
    main()
