import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


ROOT = Path(__file__).resolve().parents[1]
TEST_MARKER = "Тестовая недоступность API: проверка журнала"
STATUS = {
    "success": "Успешно",
    "error": "Ошибка",
    "running": "Выполняется",
}
SOURCES = {"parking": "Парковки", "weather": "Погода"}


def number(value):
    if type(value) is int:
        return f"{value:,}".replace(",", " ")
    return "—"


def utc_text(value):
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.notna(parsed):
        return parsed.strftime("%d.%m.%Y %H:%M:%S")
    return "—"


def duration(start, finish):
    start = pd.to_datetime(start, utc=True, errors="coerce")
    finish = pd.to_datetime(finish, utc=True, errors="coerce")
    if pd.isna(start) or pd.isna(finish) or finish < start:
        return None
    return round((finish - start).total_seconds(), 3)


def load_json(relative, issues):
    try:
        result = json.loads(
            (ROOT / relative).read_text(encoding="utf-8")
        )
        if not isinstance(result, dict):
            raise ValueError("ожидался JSON-объект")
        return result
    except (OSError, ValueError) as error:
        issues.append(f"{relative}: {error}")
        return None


def load_storage(source, issues):
    relative = f"data/storage/{source}.json"
    result = load_json(relative, issues)
    if result is None:
        return None

    if (
        not isinstance(result.get("data"), list)
        or not result.get("loaded_until_exclusive")
    ):
        issues.append(
            f"{relative}: отсутствуют записи или граница загрузки"
        )
        return None

    return {
        "count": len(result["data"]),
        "boundary": result["loaded_until_exclusive"],
    }


def load_journal(issues):
    try:
        frame = pd.read_csv(
            ROOT / "logs/load_log.csv",
            dtype=str,
            keep_default_na=False,
            encoding="utf-8-sig",
        )
        required = {
            "run_id", "started_at_utc", "finished_at_utc",
            "source", "arguments", "records_before",
            "records_after", "record_count_change",
            "boundary_before", "boundary_after", "status", "error",
        }
        if not required.issubset(frame.columns):
            raise ValueError("не хватает обязательных столбцов")

        frame["started"] = pd.to_datetime(
            frame["started_at_utc"], utc=True, errors="coerce"
        )
        frame["finished"] = pd.to_datetime(
            frame["finished_at_utc"], utc=True, errors="coerce"
        )
        if frame[["started", "finished"]].isna().any().any():
            issues.append(
                "В журнале есть некорректные временные отметки."
            )

        for field in (
            "records_before", "records_after",
            "record_count_change",
        ):
            frame[field] = pd.to_numeric(
                frame[field], errors="coerce"
            )
            if frame[field].isna().any():
                issues.append(
                    f"В журнале есть нечисловые значения: {field}."
                )

        frame["seconds"] = (
            frame["finished"] - frame["started"]
        ).dt.total_seconds()
        if frame["seconds"].lt(0).any():
            issues.append(
                "В журнале есть отрицательная длительность."
            )
            frame.loc[
                frame["seconds"] < 0, "seconds"
            ] = float("nan")

        frame["is_test"] = frame["error"].str.contains(
            TEST_MARKER, regex=False, na=False
        )
        return frame.sort_values(
            "finished", na_position="first"
        )

    except (OSError, ValueError) as error:
        issues.append(f"logs/load_log.csv: {error}")
        return pd.DataFrame()


def journal_table(frame):
    result = frame[[
        "source", "status",
        "started_at_utc", "finished_at_utc", "seconds",
        "records_before", "records_after",
        "record_count_change", "boundary_before",
        "boundary_after", "is_test", "error", "arguments",
    ]].copy()

    result["status"] = result["status"].map(
        lambda value: STATUS.get(value, value)
    )
    result["is_test"] = result["is_test"].map({
        True: "Да", False: "Нет",
    })
    result["seconds"] = result["seconds"].round(3)

    return result.rename(columns={
        "source": "Источник",
        "status": "Статус",
        "started_at_utc": "Начало UTC",
        "finished_at_utc": "Окончание UTC",
        "seconds": "Длительность, с",
        "records_before": "Записей до",
        "records_after": "Записей после",
        "record_count_change": "Изменение записей",
        "boundary_before": "Граница до",
        "boundary_after": "Граница после",
        "is_test": "Тестовый сбой",
        "error": "Ошибка",
        "arguments": "Аргументы",
    })


def main():
    st.set_page_config(
        page_title="Состояние системы парковок",
        page_icon="📊",
        layout="wide",
    )
    st.title("Состояние загрузки и обработки данных")
    st.caption(
        "Объёмы хранилищ, последние рабочие загрузки "
        "и сохранённые результаты проверок. "
        "Время запусков — UTC."
    )

    st.sidebar.header("Журнал загрузок")
    source_filter = st.sidebar.selectbox(
        "Источник", ["Все", "parking", "weather"]
    )
    status_filter = st.sidebar.selectbox(
        "Статус",
        ["Все", "success", "error"],
        format_func=lambda value: STATUS.get(value, value),
    )
    include_tests = st.sidebar.checkbox(
        "Показывать тестовые сбои", value=False
    )
    st.sidebar.button("Перечитать данные")

    issues = []
    storage = {
        source: load_storage(source, issues)
        for source in SOURCES
    }
    processing = load_json(
        "logs/processing_run.json", issues
    )
    quality = load_json(
        "logs/quality/mart_quality.json", issues
    )
    cleaning = load_json(
        "logs/quality/parking_cleaning.json", issues
    )
    schema = load_json(
        "logs/quality/schema_all.json", issues
    )
    journal = load_journal(issues)

    try:
        mart = pd.read_csv(
            ROOT / "data/marts/parking_weather.csv",
            encoding="utf-8-sig",
            usecols=["station_code"],
            dtype=str,
        )
        mart_rows = len(mart)
    except (OSError, ValueError) as error:
        issues.append(
            f"data/marts/parking_weather.csv: {error}"
        )
        mart_rows = None

    steps = processing.get("steps", []) if processing else []
    completed = sum(
        step.get("status") == "success" for step in steps
    )

    cards = st.columns(4)
    cards[0].metric(
        "Парковочных записей",
        number(
            storage["parking"]["count"]
            if storage["parking"] else None
        ),
    )
    cards[1].metric(
        "Погодных записей",
        number(
            storage["weather"]["count"]
            if storage["weather"] else None
        ),
    )
    cards[2].metric("Строк витрины", number(mart_rows))
    cards[3].metric(
        "Этапов обработки завершено",
        f"{completed}/7" if processing else "—",
    )

    st.subheader(
        "Накопительные хранилища и последние рабочие загрузки"
    )
    working = (
        journal[~journal["is_test"]]
        if not journal.empty else journal
    )
    source_rows = []

    for source, label in SOURCES.items():
        state = storage[source]
        history = (
            working[working["source"] == source]
            if not working.empty else working
        )
        last = history.iloc[-1] if not history.empty else None

        source_rows.append({
            "Источник": label,
            "Записей": state["count"] if state else None,
            "Загружено до, не включая (UTC)":
                state["boundary"] if state else "—",
            "Последний рабочий запуск UTC":
                utc_text(last["finished_at_utc"])
                if last is not None else "—",
            "Статус запуска":
                STATUS.get(last["status"], last["status"])
                if last is not None else "Нет записи",
            "Изменение записей":
                last["record_count_change"]
                if last is not None else None,
            "Ошибка запуска":
                last["error"] if last is not None else "",
        })

    st.dataframe(
        pd.DataFrame(source_rows),
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "Граница показывает покрытие исторической выгрузки. "
        "Статус загрузки и объём данных учитываются отдельно; "
        "успешный повторный запуск может добавить ноль записей."
    )

    st.subheader("Последний сохранённый запуск обработки")
    if processing:
        text = (
            "Обработка: "
            f"{STATUS.get(processing.get('status'), 'Неизвестный статус')}. "
            "Завершение UTC: "
            f"{utc_text(processing.get('finished_at_utc'))}."
        )
        if processing.get("status") == "success":
            st.success(text)
        elif processing.get("status") == "error":
            st.error(text)
        else:
            st.info(text)

        if steps:
            step_rows = [{
                "Этап": step["name"],
                "Статус": STATUS.get(
                    step.get("status"), "Нет результата"
                ),
                "Длительность, с": duration(
                    step.get("started_at_utc"),
                    step.get("finished_at_utc"),
                ),
                "Код завершения": step.get("return_code"),
                "Ошибка": step.get("error", ""),
            } for step in steps]
            st.dataframe(
                pd.DataFrame(step_rows),
                hide_index=True,
                width="stretch",
            )
    else:
        st.info("Отчёт запуска обработки недоступен.")

    st.subheader("Последние сохранённые проверки качества")
    check_rows = []

    for label, report in (
        ("Структура всех таблиц", schema),
        ("Качество витрины", quality),
    ):
        passed = report.get("passed") if report else None
        result = (
            "Пройдена" if passed is True
            else "Не пройдена" if passed is False
            else "Нет результата"
        )
        check_rows.append({
            "Проверка": label,
            "Результат": result,
        })

    st.dataframe(
        pd.DataFrame(check_rows),
        hide_index=True,
        width="stretch",
    )

    counts = quality.get("error_counts") if quality else None
    errors = (
        sum(counts.values())
        if isinstance(counts, dict)
        and all(
            type(value) is int and value >= 0
            for value in counts.values()
        )
        else None
    )

    missing = (
        cleaning.get("missing_grid_points_by_station")
        if cleaning else None
    )
    missing_total = (
        sum(missing.values())
        if isinstance(missing, dict)
        and all(
            type(value) is int and value >= 0
            for value in missing.values()
        )
        else None
    )
    limits = (
        quality.get("known_limitations", {})
        if quality else {}
    )

    cards = st.columns(4)
    cards[0].metric("Нарушений правил", number(errors))
    cards[1].metric("Пропусков сетки", number(missing_total))
    cards[2].metric(
        "Отметок вне сетки",
        number(limits.get("off_grid_records")),
    )
    cards[3].metric(
        "Вместимость не подтверждена",
        number(
            limits.get("capacity_history_unverified_records")
        ),
    )

    if (
        quality
        and mart_rows is not None
        and quality.get("mart_rows") != mart_rows
    ):
        st.warning(
            "Количество строк витрины изменилось "
            "после сохранённой проверки качества."
        )

    st.caption(
        "Пропуски сетки, отметки вне сетки и неподтверждённая "
        "историческая вместимость показаны отдельно "
        "от нарушений правил. Значения взяты "
        "из сохранённых отчётов."
    )

    if isinstance(missing, dict):
        with st.expander("Пропуски сетки по парковкам"):
            st.dataframe(
                pd.DataFrame([
                    {"Парковка": code, "Пропусков": value}
                    for code, value in missing.items()
                ]),
                hide_index=True,
                width="stretch",
            )

    if quality and counts:
        with st.expander("Нарушения качества"):
            st.json({
                "Количество": counts,
                "Примеры": quality.get("error_examples", {}),
            })

    for issue in issues:
        st.warning(
            "Недоступные или некорректные сведения: " + issue
        )

    st.subheader("Журнал запусков загрузки")
    if journal.empty:
        st.info("В журнале пока нет доступных записей.")
        return

    test_count = int(journal["is_test"].sum())
    st.caption(
        f"Всего записей в журнале: {len(journal)}. "
        f"Помеченных тестовых сбоев: {test_count}. "
        "Они доступны через переключатель слева."
    )

    filtered = (
        journal.copy() if include_tests else working.copy()
    )
    if source_filter != "Все":
        filtered = filtered[
            filtered["source"] == source_filter
        ]
    if status_filter != "Все":
        filtered = filtered[
            filtered["status"] == status_filter
        ]

    dated = filtered["started"].dropna()
    if not dated.empty:
        dates = st.sidebar.date_input(
            "Даты запусков (UTC)",
            value=(
                dated.min().date(),
                dated.max().date(),
            ),
            min_value=dated.min().date(),
            max_value=dated.max().date(),
            format="DD.MM.YYYY",
        )
        if len(dates) != 2:
            st.info(
                "Выбери начало и конец периода журнала."
            )
            return

        filtered = filtered[
            (filtered["started"].dt.date >= dates[0])
            & (filtered["started"].dt.date <= dates[1])
        ]

    if filtered.empty:
        st.info("Запусков с выбранными условиями нет.")
        return

    cards = st.columns(3)
    cards[0].metric(
        "Запусков в выборке", number(len(filtered))
    )
    cards[1].metric(
        "Запусков с ошибкой",
        number(int(filtered["status"].eq("error").sum())),
    )
    change = filtered["record_count_change"]
    cards[2].metric(
        "Изменение числа записей",
        number(int(change.sum()))
        if change.notna().all() else "—",
    )

    figure = go.Figure()
    for source in sorted(filtered["source"].unique()):
        part = filtered[
            filtered["source"] == source
        ].dropna(subset=["started", "seconds"])

        if not part.empty:
            figure.add_trace(go.Scatter(
                x=part["started"].dt.tz_localize(None),
                y=part["seconds"],
                mode="markers",
                name=SOURCES.get(source, source),
                marker={
                    "size": 11,
                    "symbol": [
                        "x" if value == "error" else "circle"
                        for value in part["status"]
                    ],
                },
                text=part["status"],
                hovertemplate=(
                    "%{x}<br>%{y:.3f} с<br>%{text}"
                    "<extra>%{fullData.name}</extra>"
                ),
            ))

    figure.update_layout(
        template="plotly_white",
        height=300,
        title="Длительность запусков загрузки",
        xaxis_title="Начало запуска (UTC)",
        yaxis_title="Секунды",
    )
    st.plotly_chart(figure, width="stretch")
    st.caption(
        "Кружок — успешный запуск, "
        "крестик — запуск с ошибкой."
    )

    table = journal_table(filtered.sort_values(
        "finished", ascending=False, na_position="last"
    ))
    st.dataframe(
        table, hide_index=True, width="stretch"
    )
    st.download_button(
        "Скачать выбранные записи журнала (CSV)",
        data=table.to_csv(index=False).encode("utf-8-sig"),
        file_name="load_journal.csv",
        mime="text/csv",
    )

    failed = filtered[filtered["status"] == "error"]
    if not failed.empty:
        with st.expander(
            "Тексты ошибок выбранных запусков",
            expanded=True,
        ):
            for _, row in failed.iterrows():
                suffix = (
                    " · тестовый сбой"
                    if row["is_test"] else ""
                )
                st.write(
                    f"{row['source']} · "
                    f"{utc_text(row['finished_at_utc'])} "
                    f"UTC{suffix}"
                )
                st.code(
                    row["error"]
                    or "Описание ошибки отсутствует.",
                    language=None,
                )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError) as error:
        st.error(
            f"Не удалось прочитать сведения о системе: {error}"
        )