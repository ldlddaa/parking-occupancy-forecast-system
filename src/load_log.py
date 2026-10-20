import csv
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "logs" / "load_log.csv"


def storage_info(path):
    """Читаем количество записей и границу загрузки."""
    if not path.exists():
        return 0, ""

    try:
        storage = json.loads(path.read_text(encoding="utf-8"))
        return (
            len(storage["data"]),
            storage["loaded_until_exclusive"],
        )
    except (OSError, ValueError, TypeError, KeyError):
        # Сам загрузчик сообщит об ошибке хранилища.
        return "", ""


def run_logged(source, storage_path, operation):
    """Запускаем загрузку и записываем результат в журнал."""
    started = datetime.now(timezone.utc).isoformat()
    before_count, before_boundary = storage_info(storage_path)

    status = "error"
    error_text = ""

    try:
        operation()
        status = "success"

    except BaseException as error:
        error_text = f"{type(error).__name__}: {error}"
        raise

    finally:
        after_count, after_boundary = storage_info(storage_path)

        if isinstance(before_count, int) and isinstance(after_count, int):
            count_change = after_count - before_count
        else:
            count_change = ""

        row = {
            "run_id": uuid.uuid4().hex,
            "started_at_utc": started,
            "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "arguments": json.dumps(
                sys.argv[1:], ensure_ascii=False
            ),
            "records_before": before_count,
            "records_after": after_count,
            "record_count_change": count_change,
            "boundary_before": before_boundary,
            "boundary_after": after_boundary,
            "status": status,
            "error": error_text,
        }

        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

        needs_header = (
            not LOG_PATH.exists() or LOG_PATH.stat().st_size == 0
        )

        with LOG_PATH.open(
            "a", encoding="utf-8", newline=""
        ) as file:
            writer = csv.DictWriter(file, fieldnames=list(row))

            if needs_header:
                writer.writeheader()

            writer.writerow(row)

        print(f"Результат запуска записан в журнал: {LOG_PATH}")