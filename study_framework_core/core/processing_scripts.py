"""
Backend processing scripts for the study framework.
Handles phone uploads, Garmin FIT ingestion, and daily summaries.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import pymongo
from geopy import distance
from pymongo.errors import BulkWriteError

from study_framework_core.core.config import get_config, set_config_file
from study_framework_core.core.handlers import get_db


# FIT record types decoded from watch uploads. ACCELEROMETER is deliberately absent;
# see the comment in process_garmin_fit_file. Add it back here to resume collection.
FIT_TYPES_TO_PROCESS = [
    "HEART_RATE",
    "RESPIRATION",
    "BBI",
    "STRESS",
    "STEPS",
    "SPO2",
    "ZERO_CROSSING",
    "FILE_METADATA",
]


class DataProcessor:
    """Main data processor for study data ingestion."""

    def __init__(self) -> None:
        self.config = get_config()
        self.db = get_db()
        self.records: Dict[str, List[Dict[str, Any]]] = {}
        self.batch_size = 2000
        self.setup_logging()
        self.init_collections()

    def setup_logging(self) -> None:
        logs_dir = Path(self.config.paths.logs_dir)
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_level = getattr(logging, self.config.logging.level.upper(), logging.INFO)
        if os.getenv("REDUCE_LOGGING", "false").lower() == "true":
            log_level = logging.WARNING
        elif os.getenv("LOG_LEVEL"):
            log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), log_level)

        logging.basicConfig(
            level=log_level,
            format=self.config.logging.format,
            handlers=[
                logging.FileHandler(logs_dir / "data_processing.log"),
                logging.StreamHandler(),
            ],
        )
        self.logger = logging.getLogger(__name__)

    def init_collections(self) -> None:
        try:
            c = self.config.collections
            self.db[c.IOS_LOCATION].create_index([("uid", 1), ("timestamp", 1), ("event_id", 1)], unique=True)
            self.db[c.IOS_WIFI].create_index([("uid", 1), ("timestamp", 1), ("event_id", 1)], unique=True)
            self.db[c.IOS_BLUETOOTH].create_index([("uid", 1), ("timestamp", 1)], unique=True)
            self.db[c.IOS_BRIGHTNESS].create_index([("uid", 1), ("timestamp", 1)], unique=True)
            self.db[c.IOS_LOCK_UNLOCK].create_index([("uid", 1), ("timestamp", 1)], unique=True)
            self.db[c.IOS_BATTERY].create_index([("uid", 1), ("timestamp", 1)], unique=True)
            self.db[c.IOS_ACTIVITY].create_index([("uid", 1), ("timestamp", 1)], unique=True)
            self.db[c.IOS_STEPS].create_index([("uid", 1), ("start_timestamp", 1)], unique=True)
            self.db[c.IOS_ACCELEROMETER].create_index([("uid", 1), ("timestamp", 1)], unique=True)
            self.db[c.IOS_CALLLOG].create_index([("uid", 1), ("timestamp", 1)], unique=True)
            self.db[c.UNKNOWN_EVENTS].create_index([("uid", 1), ("timestamp", 1)], unique=True)

            self.db[c.GARMIN_HR].create_index([("uid", 1), ("timestamp", 1)], unique=True)
            self.db[c.GARMIN_STRESS].create_index([("uid", 1), ("timestamp", 1)], unique=True)
            self.db[c.GARMIN_ACCELEROMETER].create_index([("uid", 1), ("timestamp", 1)], unique=True)
            self.db[c.GARMIN_STEPS].create_index([("uid", 1), ("timestamp", 1)], unique=True)
            self.db[c.GARMIN_RESPIRATION].create_index([("uid", 1), ("timestamp", 1)], unique=True)
            self.db[c.GARMIN_IBI].create_index([("uid", 1), ("timestamp", 1)], unique=True)
            self.db[c.GARMIN_ENERGY].create_index([("uid", 1), ("timestamp", 1)], unique=True)
            self.db[c.GARMIN_SPO2].create_index([("uid", 1), ("timestamp", 1)], unique=True)

            self.db[c.EMA_RESPONSE].create_index([("uid", 1), ("timestamp", 1)], unique=True)
            self.db[c.EMA_STATUS_EVENTS].create_index([("uid", 1), ("timestamp", 1)], unique=True)
            self.db[c.APP_USAGE_LOGS].create_index([("uid", 1), ("timestamp", 1)], unique=True)
            self.db[c.NOTIFICATION_EVENTS].create_index([("uid", 1), ("timestamp", 1)], unique=True)
            self.db[c.APP_SCREEN_EVENTS].create_index([("uid", 1), ("timestamp", 1)], unique=True)

            self.db[c.DAILY_SUMMARY].create_index([("uid", 1), ("date", 1)], unique=True)
            self.db[c.USERS].create_index([("uid", 1)], unique=True)
            self.db[c.USER_CODE_MAPPINGS].create_index([("uid", 1), ("uid_code", 1)], unique=True)
            self.db[c.USER_PINGS].create_index([("uid", 1), ("timestamp", 1)], unique=True)
            self.logger.info("Successfully initialized MongoDB indexes")
        except Exception as exc:
            self.logger.error("Error initializing collections: %s", exc)

    def add_record(self, collection: str, record: Dict[str, Any]) -> None:
        self.records.setdefault(collection, []).append(record)
        if len(self.records[collection]) >= self.batch_size:
            self.flush_records(collection)

    def flush_records(self, collection: Optional[str] = None) -> None:
        collections_to_flush = [collection] if collection else list(self.records.keys())
        for coll in collections_to_flush:
            if coll not in self.records or not self.records[coll]:
                continue
            batch = self.records[coll]
            try:
                self.db[coll].insert_many(batch, ordered=False)
                self.logger.info("Bulk inserted %d records to %s", len(batch), coll)
            except BulkWriteError as bwe:
                details = bwe.details or {}
                dup = sum(1 for e in details.get("writeErrors", []) if e.get("code") == 11000)
                total = len(details.get("writeErrors", []))
                if total > 0:
                    self.logger.warning(
                        "Bulk insert for %s had %d/%d duplicate-key rows; continuing",
                        coll,
                        dup,
                        total,
                    )
                else:
                    self.logger.error("BulkWriteError on %s: %s", coll, bwe)
            except Exception as exc:
                self.logger.error("Error flushing records to %s: %s", coll, exc)
            finally:
                self.records[coll].clear()

    def archive_file(self, user: str, file_path: str, source_type: str = "phone") -> None:
        try:
            archive_dir = Path(self.config.paths.data_processed_path) / source_type / user
            archive_dir.mkdir(parents=True, exist_ok=True)
            src = Path(file_path)
            dst = archive_dir / src.name
            if dst.exists():
                dst = archive_dir / f"{src.name}.duplicate"
            shutil.move(str(src), str(dst))
            self.logger.info("Archived file: %s -> %s", file_path, dst)
        except Exception as exc:
            self.logger.error("Error archiving file %s: %s", file_path, exc)

    def _safe_json(self, payload: Any) -> Dict[str, Any]:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="ignore")
        if isinstance(payload, str):
            try:
                loaded = json.loads(payload)
                return loaded if isinstance(loaded, dict) else {}
            except Exception:
                return {}
        return payload if isinstance(payload, dict) else {}

    def _handle_timestamp_format(self, timestamp: Any) -> float:
        try:
            ts = float(timestamp)
        except Exception:
            return 0.0
        return ts if ts < 1e12 else ts / 1000.0

    def process_phone_data(self, user: str) -> bool:
        try:
            self.logger.info("Processing phone data for user: %s", user)
            upload_path = Path(self.config.paths.data_upload_path) / "phone" / user
            if not upload_path.exists():
                fallback = Path(self.config.paths.data_upload_path) / user
                if fallback.exists():
                    upload_path = fallback
                else:
                    self.logger.warning("No upload directory found for user %s", user)
                    return False

            db_files = sorted(upload_path.glob("*.db"))
            if not db_files:
                self.logger.info("No iOS DB files found for user %s", user)
                return True

            for file_path in db_files:
                self._process_ios_database(user, file_path)

            self.flush_records()
            self.logger.info("Successfully processed phone data for user: %s", user)
            return True
        except Exception as exc:
            self.logger.error("Exception processing phone data for %s: %s", user, exc)
            return False

    def _process_ios_database(self, user: str, db_file: Path) -> None:
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(db_file)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = [row[0] for row in cursor.fetchall()]
            for table in tables:
                try:
                    cursor.execute(f"SELECT * FROM {table}")
                    rows = cursor.fetchall()
                except Exception:
                    continue
                for row in rows:
                    if len(row) < 4:
                        continue
                    event_id = row[3]
                    self._process_event_by_id(user, row, event_id)
            self.flush_records()
            self.archive_file(user, str(db_file), source_type="phone")
            self.logger.info("Successfully processed iOS database: %s", db_file)
        except Exception as exc:
            self.logger.error("Error processing iOS database %s: %s", db_file, exc)
        finally:
            if conn:
                conn.close()

    def _process_event_by_id(self, user: str, row: Tuple[Any, ...], event_id: int) -> None:
        c = self.config.collections
        ts = self._handle_timestamp_format(row[2] if len(row) > 2 else 0)
        payload = self._safe_json(row[4] if len(row) > 4 else {})
        base = {"uid": user, "timestamp": ts, "event_id": event_id, "processed_at": datetime.now().timestamp()}

        try:
            if event_id in (152, 151):
                rec = {**base, "latitude": float(payload.get("latitude", 0)), "longitude": float(payload.get("longitude", 0))}
                self.add_record(c.IOS_LOCATION, rec)
            elif event_id == 16:
                rec = {**base, "activity": payload.get("activity", payload.get("activities", [])), "confidence": payload.get("confidence", 0)}
                self.add_record(c.IOS_ACTIVITY, rec)
            elif event_id == 21:
                rec = {
                    **base,
                    "start_timestamp": ts,
                    "end_timestamp": self._handle_timestamp_format(payload.get("end_timestamp", payload.get("endTimestamp", ts))),
                    "steps": int(payload.get("steps", payload.get("stepCount", 0)) or 0),
                    "distance": float(payload.get("distance", 0) or 0),
                }
                self.add_record(c.IOS_STEPS, rec)
            elif event_id in (11, 111):
                rec = {**base, "battery_left": int(payload.get("battery_left", payload.get("batteryLevel", 0)) or 0)}
                self.add_record(c.IOS_BATTERY, rec)
            elif event_id in (18, 181):
                rec = {**base, "bssid": str(payload.get("bssid", "")), "ssid": str(payload.get("ssid", ""))}
                self.add_record(c.IOS_WIFI, rec)
            elif event_id == 19:
                rec = {**base, "bt_address": str(payload.get("bt_address", "")), "bt_name": str(payload.get("bt_name", ""))}
                self.add_record(c.IOS_BLUETOOTH, rec)
            elif event_id == 13:
                rec = {**base, "brightness": float(payload.get("brightness", 0) or 0)}
                self.add_record(c.IOS_BRIGHTNESS, rec)
            elif event_id == 14:
                rec = {**base, "lock_state": int(payload.get("LockState", payload.get("lock_state", 0)) or 0)}
                self.add_record(c.IOS_LOCK_UNLOCK, rec)
            elif event_id == 447:
                rec = {
                    **base,
                    "timestamp": self._handle_timestamp_format(payload.get("timestamp", ts)),
                    "x": float(payload.get("x", 0) or 0),
                    "y": float(payload.get("y", 0) or 0),
                    "z": float(payload.get("z", 0) or 0),
                }
                self.add_record(c.IOS_ACCELEROMETER, rec)
            elif event_id == 23:
                rec = {**base, "duration": float(payload.get("duration", 0) or 0), "callType": str(payload.get("callType", ""))}
                self.add_record(c.IOS_CALLLOG, rec)
            elif event_id == 442:
                rec = {**base, "timestamp": self._handle_timestamp_format(payload.get("timestamp", ts)), "heart_rate": float(payload.get("heart_rate", payload.get("bpm", 0)) or 0)}
                self.add_record(c.GARMIN_HR, rec)
            elif event_id == 443:
                rec = {**base, "timestamp": self._handle_timestamp_format(payload.get("timestamp", ts)), "stress": float(payload.get("stress", payload.get("stressScore", 0)) or 0)}
                self.add_record(c.GARMIN_STRESS, rec)
            elif event_id == 444:
                rec = {**base, "timestamp": self._handle_timestamp_format(payload.get("timestamp", ts)), "respiration": float(payload.get("respiration", payload.get("breathsPerMinute", 0)) or 0)}
                self.add_record(c.GARMIN_RESPIRATION, rec)
            elif event_id == 441:
                rec = {**base, "timestamp": self._handle_timestamp_format(payload.get("timestamp", ts)), "bbi": float(payload.get("bbi", 0) or 0)}
                self.add_record(c.GARMIN_IBI, rec)
            elif event_id == 445:
                rec = {
                    **base,
                    "timestamp": self._handle_timestamp_format(payload.get("startTimestamp", ts)),
                    "start_timestamp": self._handle_timestamp_format(payload.get("startTimestamp", ts)),
                    "steps_timestamp": self._handle_timestamp_format(payload.get("endTimestamp", ts)),
                    "steps": float(payload.get("stepCount", payload.get("steps", 0)) or 0),
                }
                self.add_record(c.GARMIN_STEPS, rec)
            elif event_id == 501:
                rec = {**base, "appName": str(payload.get("appName", "")), "status": str(payload.get("status", ""))}
                self.add_record(c.APP_USAGE_LOGS, rec)
            elif event_id == 502:
                rec = {**base, "ema_id": str(payload.get("ema_id", "")), "questions": payload.get("questions", {})}
                self.add_record(c.EMA_RESPONSE, rec)
            elif event_id == 503:
                rec = {**base, "ema_id": str(payload.get("ema_id", "")), "status": str(payload.get("status", ""))}
                self.add_record(c.EMA_STATUS_EVENTS, rec)
            elif event_id == 504:
                rec = {**base, "notification_id": str(payload.get("notification_id", "")), "status": str(payload.get("status", ""))}
                self.add_record(c.NOTIFICATION_EVENTS, rec)
            else:
                rec = {**base, "raw_data": str(row[4]) if len(row) > 4 else ""}
                self.add_record(c.UNKNOWN_EVENTS, rec)
        except Exception as exc:
            self.logger.error("Error processing event_id %s: %s", event_id, exc)

    def process_garmin_data(self, user: str, include_archived: bool = False) -> bool:
        try:
            self.logger.info("Processing Garmin data for user: %s", user)
            upload_path = Path(self.config.paths.data_upload_path) / "phone" / user
            if not upload_path.exists():
                fallback = Path(self.config.paths.data_upload_path) / user
                if fallback.exists():
                    upload_path = fallback
                else:
                    self.logger.warning("No upload directory found for user %s", user)
                    return False

            fit_files = sorted(upload_path.glob("*.fit"))
            if include_archived:
                archived_path = Path(self.config.paths.data_processed_path) / "phone" / user
                if archived_path.exists():
                    fit_files.extend(sorted(archived_path.glob("*.fit")))
            # de-duplicate while preserving order
            unique_fit_files: List[Path] = []
            seen: set[str] = set()
            for fit_file in fit_files:
                key = str(fit_file.resolve())
                if key in seen:
                    continue
                seen.add(key)
                unique_fit_files.append(fit_file)
            self.logger.info(
                "Found %d FIT files for %s (include_archived=%s)",
                len(unique_fit_files),
                user,
                include_archived,
            )
            for fit_file in unique_fit_files:
                ok = self.process_garmin_fit_file(user, str(fit_file))
                if ok:
                    # Only archive files from upload dir; archived replays should stay in place.
                    try:
                        is_under_upload = upload_path.resolve() in fit_file.resolve().parents
                    except Exception:
                        is_under_upload = str(fit_file).startswith(str(upload_path))
                    if is_under_upload:
                        self.archive_file(user, str(fit_file), source_type="phone")
            return True
        except Exception as exc:
            self.logger.error("Exception processing Garmin data for %s: %s", user, exc)
            return False

    def process_garmin_fit_file(self, user: str, input_file: str) -> bool:
        try:
            jar_path = Path(__file__).parent / "processing" / "load_files" / "fit-processing-cli.jar"
            input_path = Path(input_file)
            if not jar_path.exists():
                self.logger.error("JAR file not found: %s", jar_path)
                return False
            if not input_path.exists():
                self.logger.error("Input file not found: %s", input_path)
                return False

            output_path = Path(self.config.paths.data_processed_path) / "garmin" / user
            output_path.mkdir(parents=True, exist_ok=True)
            csv_subdir = output_path / f"{input_path.stem}_csv_out"
            # Decode only the metrics this study uses. Accelerometer was 96% of the decoded
            # output (887 KB of 920 KB from one 128 KB FIT file) and nothing reads it: the
            # dashboard's charts are heart rate, respiration, HRV and SpO2. Leaving it out
            # here rather than dropping it at insert time also saves the decode and the
            # temporary CSV, not just the database write.
            cmd = [
                "java", "-jar", str(jar_path), str(input_path),
                "--output_file", str(output_path), "--output_format", "CSV",
                "--types_to_process", ",".join(FIT_TYPES_TO_PROCESS),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                self.logger.error("FIT processing failed for %s: %s", input_path, result.stderr)
                return False
            return self._process_garmin_csv_files(user, csv_subdir)
        except Exception as exc:
            self.logger.error("Exception processing Garmin file %s: %s", input_file, exc)
            return False

    def _process_garmin_csv_files(self, user: str, csv_directory: Path) -> bool:
        try:
            if not csv_directory.exists() or not csv_directory.is_dir():
                self.logger.error("CSV directory not found: %s", csv_directory)
                return False

            records_by_collection: Dict[str, List[Dict[str, Any]]] = {}
            for csv_file in csv_directory.glob("*.csv"):
                try:
                    df = pd.read_csv(csv_file)
                except pd.errors.EmptyDataError:
                    continue
                except Exception as exc:
                    self.logger.error("Error reading %s: %s", csv_file.name, exc)
                    continue

                for row in df.itertuples(index=False):
                    parsed: Optional[Tuple[str, Dict[str, Any]]] = None
                    if "ACCELEROMETER" in csv_file.name:
                        parsed = self._handle_garmin_accelerometer(user, row)
                    elif "BBI" in csv_file.name:
                        parsed = self._handle_garmin_ibi(user, row)
                    elif "HEART_RATE" in csv_file.name:
                        parsed = self._handle_garmin_hr(user, row)
                    elif "RESPIRATION" in csv_file.name:
                        parsed = self._handle_garmin_respiration(user, row)
                    elif (
                        "SPO2" in csv_file.name
                        or "PULSE_OX" in csv_file.name
                        or "BLOOD_OXYGEN" in csv_file.name
                        or "PULSE_OXIMETRY" in csv_file.name
                    ):
                        parsed = self._handle_garmin_spo2(user, row)
                    elif "STEPS" in csv_file.name:
                        parsed = self._handle_garmin_steps(user, row)
                    elif "STRESS" in csv_file.name:
                        parsed = self._handle_garmin_stress(user, row)
                    if not parsed:
                        continue
                    coll, rec = parsed
                    records_by_collection.setdefault(coll, []).append(rec)

            for coll, rows in records_by_collection.items():
                if not rows:
                    continue
                try:
                    self.db[coll].insert_many(rows, ordered=False)
                    self.logger.info("Inserted %d Garmin rows into %s", len(rows), coll)
                except BulkWriteError as bwe:
                    details = bwe.details or {}
                    dup = sum(1 for e in details.get("writeErrors", []) if e.get("code") == 11000)
                    total = len(details.get("writeErrors", []))
                    self.logger.warning("Garmin insert for %s had %d/%d duplicates", coll, dup, total)
                except Exception as exc:
                    self.logger.error("Error inserting Garmin rows into %s: %s", coll, exc)

            shutil.rmtree(csv_directory, ignore_errors=True)
            return True
        except Exception as exc:
            self.logger.error("Exception processing Garmin CSV files: %s", exc)
            return False

    def _handle_garmin_accelerometer(self, user: str, row: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
        try:
            ts = float(getattr(row, "timestamp", 0)) + float(getattr(row, "micros", 0)) / 1_000_000
            rec = {"uid": user, "event_id": 447, "timestamp": ts, "x": float(getattr(row, "x", 0)), "y": float(getattr(row, "y", 0)), "z": float(getattr(row, "z", 0)), "processed_at": datetime.now().timestamp()}
            return self.config.collections.GARMIN_ACCELEROMETER, rec
        except Exception:
            return None

    def _handle_garmin_ibi(self, user: str, row: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
        try:
            ts = float(getattr(row, "timestamp", 0)) + float(getattr(row, "millis", 0)) / 1_000
            rec = {"uid": user, "event_id": 441, "timestamp": ts, "bbi": float(getattr(row, "bbi", 0)), "processed_at": datetime.now().timestamp()}
            return self.config.collections.GARMIN_IBI, rec
        except Exception:
            return None

    def _handle_garmin_hr(self, user: str, row: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
        try:
            rec = {"uid": user, "event_id": 442, "timestamp": float(getattr(row, "timestamp", 0)), "heart_rate": float(getattr(row, "bpm", 0)), "status": str(getattr(row, "status", "")), "processed_at": datetime.now().timestamp()}
            return self.config.collections.GARMIN_HR, rec
        except Exception:
            return None

    def _handle_garmin_respiration(self, user: str, row: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
        try:
            ts = float(getattr(row, "timestamp", 0))
            rec = {"uid": user, "event_id": 444, "timestamp": ts, "respiration_timestamp": ts, "respiration": float(getattr(row, "breathsPerMinute", 0)), "status": str(getattr(row, "respirationStatus", "")), "processed_at": datetime.now().timestamp()}
            return self.config.collections.GARMIN_RESPIRATION, rec
        except Exception:
            return None

    def _handle_garmin_steps(self, user: str, row: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
        try:
            rec = {
                "uid": user,
                "event_id": 445,
                "timestamp": float(getattr(row, "startTimestamp", 0)),
                "start_timestamp": float(getattr(row, "startTimestamp", 0)),
                "steps_timestamp": float(getattr(row, "endTimestamp", 0)),
                "steps": float(getattr(row, "stepCount", 0)),
                "total_steps": float(getattr(row, "totalSteps", 0)),
                "processed_at": datetime.now().timestamp(),
            }
            return self.config.collections.GARMIN_STEPS, rec
        except Exception:
            return None

    def _handle_garmin_stress(self, user: str, row: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
        try:
            rec = {
                "uid": user,
                "event_id": 443,
                "timestamp": float(getattr(row, "timestamp", 0)),
                "heart_rate": float(getattr(row, "stressScore", 0)),
                "status": str(getattr(row, "stressStatus", "")),
                "average_stress_intensity": float(getattr(row, "averageStressIntensity", 0)),
                "body_battery": float(getattr(row, "bodyBattery", 0)),
                "body_battery_status": str(getattr(row, "bodyBatteryStatus", "")),
                "processed_at": datetime.now().timestamp(),
            }
            return self.config.collections.GARMIN_STRESS, rec
        except Exception:
            return None

    def _handle_garmin_spo2(self, user: str, row: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
        try:
            # Handle multiple CSV schemas from different Garmin devices/export versions.
            raw_ts = (
                getattr(row, "timestamp", None)
                or getattr(row, "spo2Timestamp", None)
                or getattr(row, "measurementTimestamp", None)
                or 0
            )
            ts = float(raw_ts)

            raw_spo2 = (
                # spO2Reading is what fit-processing-cli emits. None of the names below
                # ever matched it, so garmin_spo2 stayed empty for every participant.
                getattr(row, "spO2Reading", None)
                or getattr(row, "spo2", None)
                or getattr(row, "SpO2", None)
                or getattr(row, "oxygenSaturation", None)
                or getattr(row, "bloodOxygen", None)
                or getattr(row, "pulseOx", None)
                or 0
            )
            spo2_value = float(raw_spo2)
            # The watch writes a row every 10 s and most say "null", which pandas reads
            # as NaN. NaN is truthy and not <= 0, so without isfinite it would be stored.
            if not math.isfinite(spo2_value) or spo2_value <= 0:
                return None

            rec = {
                "uid": user,
                "event_id": 446,
                "timestamp": ts,
                "spo2": spo2_value,
                "oxygen_saturation": spo2_value,
                "status": str(
                    getattr(row, "status", None)
                    or getattr(row, "spo2Status", None)
                    or ""
                ),
                "processed_at": datetime.now().timestamp(),
            }
            return self.config.collections.GARMIN_SPO2, rec
        except Exception:
            return None

    def generate_daily_summaries(self, date: Optional[str] = None, force_user: Optional[str] = None) -> bool:
        try:
            if date is None:
                now = datetime.now()
                target_date = now.date()
                start_ts = int(datetime.combine(target_date, datetime.min.time()).timestamp())
                end_ts = int(now.timestamp())
            else:
                target_date = datetime.strptime(date, "%Y-%m-%d").date()
                start_ts = int(datetime.combine(target_date, datetime.min.time()).timestamp())
                end_ts = int(datetime.combine(target_date, datetime.max.time()).timestamp())

            if force_user:
                users = [{"uid": force_user}]
            else:
                users = list(
                    self.db[self.config.collections.USERS].find(
                        {"$or": [{"ios_login_time": {"$exists": True}}, {"garmin_login_time": {"$exists": True}}]},
                        {"uid": 1},
                    )
                )

            for user_doc in users:
                self._generate_user_daily_summary(user_doc["uid"], start_ts, end_ts, target_date)
            self.logger.info("Generated daily summaries for %s", target_date)
            return True
        except Exception as exc:
            self.logger.error("Exception generating daily summaries: %s", exc)
            return False

    def generate_summaries_for_period(self, days_back: int = 7, force_user: Optional[str] = None) -> bool:
        success = True
        for i in range(days_back):
            if i == 0:
                success &= self.generate_daily_summaries(force_user=force_user)
            else:
                target = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                success &= self.generate_daily_summaries(date=target, force_user=force_user)
        return success

    def _generate_user_daily_summary(self, uid: str, start_ts: int, end_ts: int, target_date: datetime.date) -> None:
        location_distance, location_duration = self._get_location_info(uid, start_ts, end_ts)
        wear_duration, on_duration = self._get_garmin_info(uid, start_ts, end_ts)
        ema_info = self._get_ema_info(uid, start_ts, end_ts)
        summary = {
            "uid": uid,
            "date": start_ts,
            "date_str": target_date.strftime("%Y-%m-%d"),
            "location": {"distance_traveled": location_distance, "duration_hours": location_duration},
            "garmin_wear_duration": wear_duration,
            "garmin_on_duration": on_duration,
            "ema": ema_info,
            "generated_at": datetime.now().timestamp(),
        }
        self.db[self.config.collections.DAILY_SUMMARY].update_one({"uid": uid, "date": start_ts}, {"$set": summary}, upsert=True)

    def _get_location_info(self, uid: str, start_ts: int, end_ts: int) -> Tuple[float, float]:
        try:
            rows = list(self.db[self.config.collections.IOS_LOCATION].find({"uid": uid, "event_id": 152, "timestamp": {"$gte": start_ts, "$lt": end_ts}}).sort("timestamp", 1))
            if not rows:
                return 0.0, 0.0
            distance_m = self._calculate_distance_traveled(rows)
            gps_minutes = 0.0
            prev = 0.0
            for row in rows:
                ts = float(row["timestamp"])
                if ts - prev < 15 * 60:
                    gps_minutes += (ts - prev) / 60.0
                prev = ts
            return distance_m, gps_minutes / 60.0
        except Exception:
            return 0.0, 0.0

    def _calculate_distance_traveled(self, gps_records: List[Dict[str, Any]]) -> float:
        total = 0.0
        if len(gps_records) < 2:
            return total
        for i in range(1, len(gps_records)):
            prev = gps_records[i - 1]
            cur = gps_records[i]
            try:
                total += distance.distance((prev.get("latitude", 0), prev.get("longitude", 0)), (cur.get("latitude", 0), cur.get("longitude", 0))).meters
            except Exception:
                continue
        return total

    def _get_garmin_info(self, uid: str, start_ts: int, end_ts: int) -> Tuple[float, float]:
        try:
            hr_rows = list(self.db[self.config.collections.GARMIN_HR].find({"uid": uid, "timestamp": {"$gte": start_ts, "$lt": end_ts}}))
            hr_count = sum(1 for row in hr_rows if row.get("heart_rate", 0) > 0)
            stress_count = self.db[self.config.collections.GARMIN_STRESS].count_documents({"uid": uid, "timestamp": {"$gte": start_ts, "$lt": end_ts}})
            return float(hr_count) / (6 * 60), float(stress_count) / (6 * 60)
        except Exception:
            return 0.0, 0.0

    def _get_ema_info(self, uid: str, start_ts: int, end_ts: int) -> Dict[str, Any]:
        try:
            responses = list(self.db[self.config.collections.EMA_RESPONSE].find({"uid": uid, "timestamp": {"$gte": start_ts, "$lt": end_ts}}))
            scheduled = list(self.db[self.config.collections.EMA_STATUS_EVENTS].find({"uid": uid, "timestamp": {"$gte": start_ts, "$lt": end_ts}, "status": "scheduled"}))
            return {"responses": responses, "scheduled": scheduled, "response_count": len(responses), "scheduled_count": len(scheduled)}
        except Exception:
            return {"responses": [], "scheduled": [], "response_count": 0, "scheduled_count": 0}


def process_all_data() -> None:
    processor = DataProcessor()
    users = processor.db[processor.config.collections.USERS].find({}, {"uid": 1})
    for user_doc in users:
        uid = user_doc["uid"]
        # Keep Garmin ingestion in the dedicated process_garmin action.
        processor.process_phone_data(uid)
    processor.generate_daily_summaries()


def generate_all_summaries() -> None:
    DataProcessor().generate_daily_summaries()


def _ensure_config_for_cli() -> None:
    if "STUDY_CONFIG_FILE" in os.environ:
        return
    current_dir = Path.cwd()
    config_file: Optional[Path] = None
    if (current_dir / "config" / "study_config.json").exists():
        config_file = current_dir / "config" / "study_config.json"
    else:
        for parent in current_dir.parents:
            candidate = parent / "config" / "study_config.json"
            if candidate.exists():
                config_file = candidate
                break
    if config_file:
        os.environ["STUDY_CONFIG_FILE"] = str(config_file)
        set_config_file(str(config_file))
        print(f"Set STUDY_CONFIG_FILE to: {config_file}")


def process_garmin_files() -> None:
    _ensure_config_for_cli()
    processor = DataProcessor()
    users = processor.db[processor.config.collections.USERS].find({}, {"uid": 1})
    for user_doc in users:
        processor.process_garmin_data(user_doc["uid"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Study Framework Data Processing")
    parser.add_argument("--action", choices=["process_data", "generate_summaries", "process_garmin"], required=True)
    parser.add_argument("--user")
    parser.add_argument("--date")
    parser.add_argument("--include-archived", action="store_true")
    args = parser.parse_args()

    if args.action == "process_data":
        if args.user:
            DataProcessor().process_phone_data(args.user)
        else:
            process_all_data()
    elif args.action == "generate_summaries":
        processor = DataProcessor()
        if args.date:
            processor.generate_daily_summaries(args.date)
        else:
            processor.generate_summaries_for_period(days_back=7)
    elif args.action == "process_garmin":
        if args.user:
            DataProcessor().process_garmin_data(args.user, include_archived=args.include_archived)
        else:
            if args.include_archived:
                processor = DataProcessor()
                users = processor.db[processor.config.collections.USERS].find({}, {"uid": 1})
                for user_doc in users:
                    processor.process_garmin_data(user_doc["uid"], include_archived=True)
            else:
                process_garmin_files()
