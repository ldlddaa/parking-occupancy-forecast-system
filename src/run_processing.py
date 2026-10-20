import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "logs" / "processing_run.json"

STEPS = [
    (
        "Проверка структуры входных данных",
        "check_schema.py",
        ["--stage", "raw"],
    ),
    (
        "Проверка справочника парковок",
        "load_reference.py",
        [],
    ),
    (
        "Обработка парковочных данных",
        "prepare_parking.py",
        [],
    ),
    (
        "Обработка погодных данных",
        "prepare_weather.py",
        [],
    ),
    (
        "Построение аналитической витрины",
        "build_mart.py",
        [],
    ),
    (
        "Проверка структуры всех таблиц",
        "check_schema.py",
        [],
    ),
    (
        "Проверка качества витрины",
        "check_quality.py",
        [],
    ),
]


def now():
    return datetime.now(timezone.utc).isoformat()


def save_report(report):
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = REPORT_PATH.with_suffix(".tmp")

    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(REPORT_PATH)


def main():
    report = {
        "run_id": uuid.uuid4().hex,
        "started_at_utc": now(),
        "finished_at_utc": None,
        "status": "running",
        "steps": [],
    }
    save_report(report)

    for number, (title, script, arguments) in enumerate(STEPS, start=1):
        command = [
            sys.executable,
            str(ROOT / "src" / script),
            *arguments,
        ]

        step = {
            "name": title,
            "script": script,
            "arguments": arguments,
            "started_at_utc": now(),
            "finished_at_utc": None,
            "status": "running",
            "return_code": None,
            "error": "",
        }

        report["steps"].append(step)
        save_report(report)

        print(f"\nШАГ {number}/{len(STEPS)}: {title}", flush=True)

        try:
            result = subprocess.run(command, cwd=ROOT)
            step["return_code"] = result.returncode

            if result.returncode == 0:
                step["status"] = "success"
            else:
                step["status"] = "error"
                step["error"] = (
                    f"Скрипт завершился с кодом {result.returncode}"
                )

        except (OSError, KeyboardInterrupt) as error:
            step["status"] = "error"
            step["error"] = f"{type(error).__name__}: {error}"

        step["finished_at_utc"] = now()

        if step["status"] != "success":
            report["status"] = "error"
            report["finished_at_utc"] = now()
            save_report(report)

            print(
                f"\nОбработка остановлена на этапе: {title}",
                flush=True,
            )
            print("Следующие этапы не запускались.")
            print("Описание ошибки:", step["error"])
            print("Отчёт запуска:", REPORT_PATH)
            return 1

        save_report(report)

    report["status"] = "success"
    report["finished_at_utc"] = now()
    save_report(report)

    print("\nВсе 7 этапов обработки завершены успешно.")
    print("Витрина: data/marts/parking_weather.csv")
    print("Отчёт запуска:", REPORT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())