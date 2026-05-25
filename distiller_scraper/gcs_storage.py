"""
GCS 儲存輔助模組

負責 Cloud Run 環境中 SQLite 資料庫與 CSV 檔案的 GCS 上傳/下載。
本機開發時不設定 GCS_BUCKET 環境變數，此模組不會被呼叫。
"""

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


def download_db(bucket_name: str, blob_name: str, local_path: str) -> bool:
    """從 GCS 下載 SQLite 資料庫。首次部署（檔案不存在）時自動建立空白 DB。

    Returns:
        True  — 成功從 GCS 下載
        False — 下載失敗（已建立空白 DB）
    """
    try:
        from google.cloud import storage  # type: ignore[import]

        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(local_path)
        logger.info("已從 GCS 下載 %s/%s → %s", bucket_name, blob_name, local_path)
        return True
    except Exception as exc:
        logger.warning("GCS 下載失敗（%s/%s），建立空白 DB：%s", bucket_name, blob_name, exc)
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(local_path)
        conn.close()
        return False


def upload_db(bucket_name: str, blob_name: str, local_path: str) -> bool:
    """將本機 SQLite 資料庫上傳至 GCS。

    Returns:
        True  — 上傳成功
        False — 上傳失敗
    """
    try:
        from google.cloud import storage  # type: ignore[import]

        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(local_path)
        logger.info("已上傳 %s → GCS %s/%s", local_path, bucket_name, blob_name)
        return True
    except Exception as exc:
        logger.error("GCS 上傳失敗（%s/%s）：%s", bucket_name, blob_name, exc)
        return False


def upload_csv(bucket_name: str, blob_prefix: str, local_path: str) -> bool:
    """將 CSV 備份上傳至 GCS。blob 路徑 = prefix + 原始檔名。

    Returns:
        True  — 上傳成功
        False — 上傳失敗
    """
    try:
        from google.cloud import storage  # type: ignore[import]

        filename = Path(local_path).name
        blob_name = f"{blob_prefix.rstrip('/')}/{filename}"
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(local_path)
        logger.info("已上傳 CSV %s → GCS %s/%s", local_path, bucket_name, blob_name)
        return True
    except Exception as exc:
        logger.error("CSV 上傳失敗：%s", exc)
        return False


def get_blob_updated_time(
    bucket_name: str, blob_name: str
) -> "datetime.datetime | None":
    """取得 GCS blob 的最後更新時間（UTC）。

    Returns:
        datetime（含時區） — 成功取得
        None             — 失敗或 blob 不存在
    """
    import datetime

    try:
        from google.cloud import storage  # type: ignore[import]

        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        blob.reload()  # 取得最新 metadata
        return blob.updated  # datetime with tzinfo=UTC
    except Exception as exc:
        logger.warning(
            "GCS 取得 blob 更新時間失敗（%s/%s）：%s", bucket_name, blob_name, exc
        )
        return None
