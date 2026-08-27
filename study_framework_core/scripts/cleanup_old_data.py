#!/usr/bin/env python3
"""
Periodic cleanup for MongoDB time-series data and local log files.

Retention policy:
- MongoDB: delete documents older than RETENTION_DAYS from selected collections.
- Logs: delete files in the main logs directory older than RETENTION_DAYS.

This script is intended to be run frequently (e.g., every 5 minutes) from cron.
"""

import datetime as _dt
import json as _json
import os as _os
import sys as _sys
from typing import Dict, Tuple


RETENTION_DAYS = int(_os.getenv("STUDY_RETENTION_DAYS", "7"))

BASE_DIR = _os.path.abspath(
    _os.getenv(
        "STUDY_BASE_DIR",
        "/home/ubuntu/wearable/ubiwell-study-backend-core",
    )
)
CONFIG_PATH = _os.path.join(BASE_DIR, "config", "study_config.json")
LOGS_DIR = _os.path.join(BASE_DIR, "logs")

UBIWELL_BAK_SITE_PACKAGES = _os.path.join(
    BASE_DIR,
    "ubiwell_bak",
    "lib",
    "python3.12",
    "site-packages",
)


def _ensure_pymongo():
    if UBIWELL_BAK_SITE_PACKAGES not in _sys.path and _os.path.isdir(
        UBIWELL_BAK_SITE_PACKAGES
    ):
        _sys.path.insert(0, UBIWELL_BAK_SITE_PACKAGES)
    try:
        from pymongo import MongoClient  # type: ignore  # noqa: F401
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[cleanup] WARN: pymongo not available: {exc}", file=_sys.stderr)
        return False
    return True


def _get_mongo_client():
    from urllib.parse import quote as _quote  # type: ignore
    from pymongo import MongoClient  # type: ignore

    with open(CONFIG_PATH, "r") as handle:
        cfg = _json.load(handle)

    db_cfg = cfg.get("database", {})
    user = _quote(str(db_cfg.get("username", "")))
    password = _quote(str(db_cfg.get("password", "")))
    host = db_cfg.get("host", "127.0.0.1")
    port = db_cfg.get("port", 27017)
    dbname = db_cfg.get("database", "study_db")
    auth_source = _quote(str(db_cfg.get("auth_source", "admin")))
    auth_mech = _quote(str(db_cfg.get("auth_mechanism", "SCRAM-SHA-1")))

    uri = (
        f"mongodb://{user}:{password}@{host}:{port}/{dbname}"
        f"?authSource={auth_source}&authMechanism={auth_mech}"
    )
    client = MongoClient(uri)
    return client, dbname


def _collections_and_time_fields() -> Dict[str, str]:
    """
    Collections that can safely be pruned based on a time field.

    Only high-volume time-series collections are included.
    Admin and configuration collections are intentionally excluded.
    """
    return {
        # Wearable (Garmin) streams
        "garmin_hr": "timestamp",
        "garmin_respiration": "timestamp",
        "garmin_ibi": "timestamp",
        "garmin_steps": "timestamp",
        "garmin_stress": "timestamp",
        "garmin_energy": "timestamp",
        "garmin_accelerometer": "timestamp",
        # Phone (iOS) and app streams
        "ios_accelerometer": "timestamp",
        "ios_activity": "timestamp",
        "ios_battery": "timestamp",
        "ios_bluetooth": "timestamp",
        "ios_brightness": "timestamp",
        "ios_calllog": "timestamp",
        "ios_location": "timestamp",
        "ios_lock_unlock": "timestamp",
        "ios_steps": "start_timestamp",
        "ios_wifi": "timestamp",
        "app_screen_events": "timestamp",
        "app_usage_logs": "timestamp",
        "ema_response": "timestamp",
        "ema_status_events": "timestamp",
        "notification_events": "timestamp",
        "unknown_events_data": "timestamp",
        "user_pings": "timestamp",
        # daily_summaries is intentionally not pruned here.
    }


def _infer_unit(sample_value: int) -> Tuple[bool, str]:
    """
    Infer whether a numeric timestamp is in seconds or milliseconds.
    """
    if not isinstance(sample_value, (int, float)):
        return False, "unknown"
    if sample_value > 1_000_000_000_000:
        return True, "ms"
    return False, "s"


def cleanup_mongo():
    # MongoDB data cleanup disabled: do not delete documents older than RETENTION_DAYS.
    # Remove this return to re-enable cleanup.
    return

    if not _ensure_pymongo():
        return

    try:
        client, dbname = _get_mongo_client()
    except Exception as exc:
        print(f"[cleanup] ERROR: Failed to create Mongo client: {exc}", file=_sys.stderr)
        return

    try:
        db = client[dbname]
        cutoff_dt = _dt.datetime.utcnow() - _dt.timedelta(days=RETENTION_DAYS)
        cutoff_ts_sec = int(cutoff_dt.timestamp())

        coll_cfg = _collections_and_time_fields()

        for coll_name, time_field in coll_cfg.items():
            coll = db[coll_name]

            sample = coll.find_one({time_field: {"$exists": True}}, {time_field: 1})
            if not sample:
                continue

            raw_value = sample.get(time_field)
            if not isinstance(raw_value, (int, float)):
                continue

            is_millis, unit = _infer_unit(raw_value)
            if is_millis:
                cutoff = cutoff_ts_sec * 1000
            else:
                cutoff = cutoff_ts_sec

            try:
                result = coll.delete_many({time_field: {"$lt": cutoff}})
            except Exception as exc:
                print(
                    f"[cleanup] ERROR deleting from {coll_name}: {exc}",
                    file=_sys.stderr,
                )
                continue

            deleted = getattr(result, "deleted_count", None)
            if deleted:
                print(
                    f"[cleanup] Mongo: {coll_name} - deleted {deleted} docs "
                    f"(time_field={time_field}, unit={unit}, cutoff={cutoff})"
                )
    finally:
        try:
            client.close()
        except Exception:
            pass


def cleanup_logs():
    if not _os.path.isdir(LOGS_DIR):
        return

    now = _dt.datetime.utcnow()
    cutoff = now - _dt.timedelta(days=RETENTION_DAYS)
    cutoff_ts = cutoff.timestamp()

    removed_files = 0
    for root, _dirs, files in _os.walk(LOGS_DIR):
        for name in files:
            path = _os.path.join(root, name)
            try:
                st = _os.stat(path)
            except FileNotFoundError:
                continue
            mtime = st.st_mtime
            if mtime < cutoff_ts:
                try:
                    _os.remove(path)
                    removed_files += 1
                except Exception as exc:
                    print(
                        f"[cleanup] WARN: Failed to remove log file {path}: {exc}",
                        file=_sys.stderr,
                    )
    if removed_files:
        print(f"[cleanup] Logs: removed {removed_files} files older than {RETENTION_DAYS} days")


def main():
    cleanup_mongo()
    cleanup_logs()


if __name__ == "__main__":
    main()

