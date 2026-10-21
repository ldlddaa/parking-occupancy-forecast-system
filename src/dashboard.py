import hashlib
import json
import math
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


ROOT = Path(__file__).resolve().parents[1]
LABELS = {
    "persistence": "Простой прогноз",
    "linear_regression": "Линейная регрессия",
    "random_forest": "Случайный лес",
}
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_mart(zone):
    frame = pd.read_csv(
        ROOT / "data/marts/parking_weather.csv",
        encoding="utf-8-sig",
        dtype={
            "station_code": str,
            "capacity_history_verified": str,
        },
    )
    required = {
        "station_code", "station_name", "timestamp_utc",
        "occupied", "capacity", "occupancy_pct",
        "capacity_history_verified", "is_on_grid",
    }
    if frame.empty or not required.issubset(frame.columns):
        raise ValueError(
            "Витрина пуста или имеет неподходящую структуру."
        )

    frame["timestamp_utc"] = pd.to_datetime(
        frame["timestamp_utc"], utc=True, format="ISO8601"
    )
    if frame.duplicated(["station_code", "timestamp_utc"]).any():
        raise ValueError("В витрине есть повторяющиеся наблюдения.")

    frame["occupancy_pct"] = pd.to_numeric(
        frame["occupancy_pct"], errors="raise"
    )
    if not frame["occupancy_pct"].between(0, 100).all():
        raise ValueError(
            "Загрузка должна находиться в диапазоне 0–100%."
        )

    frame["local_time"] = frame["timestamp_utc"].dt.tz_convert(zone)
    return frame.sort_values(["station_code", "timestamp_utc"])


def load_predictions(zone):
    report = read_json(ROOT / "reports/models/test_report.json")
    manifest_path = ROOT / "models/forecast_manifest.json"

    if (
        report["status"] != "success"
        or file_hash(manifest_path) != report["model_manifest_sha256"]
    ):
        raise ValueError(
            "Результаты проверки не соответствуют сохранённым моделям."
        )

    manifest = read_json(manifest_path)
    for name, digest in manifest["input_files_sha256"].items():
        if file_hash(ROOT / name) != digest:
            raise ValueError(
                "Данные или настройки изменились после обучения."
            )

    info = report["artifacts"]["predictions"]
    path = ROOT / info["path"]
    if file_hash(path) != info["sha256"]:
        raise ValueError(
            "Сохранённые прогнозы изменились после проверки."
        )

    frame = pd.read_csv(
        path, encoding="utf-8-sig", dtype={"station_code": str}
    )
    for field in ("timestamp_utc", "target_timestamp_utc"):
        frame[field] = pd.to_datetime(
            frame[field], utc=True, format="ISO8601"
        )

    if (
        len(frame) != report["test_rows"]
        or frame.duplicated(
            ["station_code", "target_timestamp_utc"]
        ).any()
    ):
        raise ValueError(
            "Не совпадает количество или уникальность прогнозов."
        )

    for field in (
        "actual_occupancy_pct", "persistence_pct",
        "linear_regression_pct", "random_forest_pct",
        "selected_prediction_pct",
    ):
        frame[field] = pd.to_numeric(frame[field], errors="raise")
        if not frame[field].between(0, 100).all():
            raise ValueError(
                "Сохранённый прогноз содержит некорректные значения."
            )

    for code, station in report["stations"].items():
        part = frame[frame["station_code"] == code]
        selected = station["selected_model_by_validation"]
        if (
            selected not in LABELS
            or len(part) != station["test_rows"]
            or set(part["selected_model"]) != {selected}
        ):
            raise ValueError(
                "Выбранная модель не соответствует отчёту."
            )

        difference = (
            part["selected_prediction_pct"]
            - part[selected + "_pct"]
        ).abs()
        if not difference.le(1e-8).all():
            raise ValueError(
                "Не совпадают значения выбранного прогноза."
            )

    frame["target_local"] = (
        frame["target_timestamp_utc"].dt.tz_convert(zone)
    )
    return frame, report


def period_filter(frame, column, start, end):
    dates = frame[column].dt.date
    return frame[(dates >= start) & (dates <= end)].copy()


def line_trace(
    frame, time_column, value_column, name,
    color, interval, zone, dash="solid",
):
    times, values = [], []
    previous = None

    for timestamp, value in zip(
        frame[time_column], frame[value_column]
    ):
        if previous is not None and timestamp - previous > interval:
            times.append(
                (previous + interval).tz_convert(zone).tz_localize(None)
            )
            values.append(None)

        times.append(timestamp.tz_convert(zone).tz_localize(None))
        values.append(float(value))
        previous = timestamp

    return go.Scatter(
        x=times,
        y=values,
        mode="lines",
        name=name,
        connectgaps=False,
        line={"color": color, "width": 2, "dash": dash},
        hovertemplate=(
            "%{x|%d.%m.%Y %H:%M}<br>%{y:.2f}%"
            "<extra>%{fullData.name}</extra>"
        ),
    )


def style_chart(figure, y_title, height=380):
    figure.update_layout(
        template="plotly_white",
        height=height,
        margin={"l": 20, "r": 20, "t": 30, "b": 20},
        hovermode="x unified",
        yaxis_title=y_title,
        legend={"orientation": "h", "y": 1.15, "x": 0},
    )
    return figure


def main():
    st.set_page_config(
        page_title="Парковки Больцано",
        page_icon="🅿️",
        layout="wide",
    )
    st.title("Загрузка парковок Больцано")
    st.caption(
        "Исторические наблюдения и проверка прогнозов "
        "на один час вперёд."
    )

    processing = read_json(ROOT / "config/processing.json")
    zone = ZoneInfo(processing["calendar_timezone"])
    interval = pd.Timedelta(
        seconds=processing["parking_period_seconds"]
    )
    mart = load_mart(zone)

    predictions = None
    test_report = None
    forecast_problem = None
    try:
        predictions, test_report = load_predictions(zone)
    except (OSError, ValueError, TypeError, KeyError) as error:
        forecast_problem = str(error)

    names = (
        mart.groupby("station_code")["station_name"]
        .first().to_dict()
    )

    st.sidebar.header("Фильтры")
    code = st.sidebar.selectbox(
        "Парковка",
        sorted(names),
        format_func=lambda value: f"{value} — {names[value]}",
    )
    station = mart[mart["station_code"] == code].copy()
    first_date = station["local_time"].dt.date.min()
    last_date = station["local_time"].dt.date.max()

    station_predictions = None
    if predictions is not None:
        station_predictions = predictions[
            predictions["station_code"] == code
        ].sort_values("target_timestamp_utc")

    default_start = max(
        first_date, last_date - timedelta(days=6)
    )
    if (
        station_predictions is not None
        and not station_predictions.empty
    ):
        default_start = max(
            first_date,
            station_predictions["target_local"].dt.date.min(),
        )

    selected_dates = st.sidebar.date_input(
        "Период",
        value=(default_start, last_date),
        min_value=first_date,
        max_value=last_date,
        format="DD.MM.YYYY",
    )
    show_baseline = st.sidebar.checkbox(
        "Показать простой прогноз", value=True
    )
    show_both = st.sidebar.checkbox(
        "Показать обе обученные модели", value=False
    )
    st.sidebar.button("Перечитать данные")
    st.sidebar.caption(f"Даты и часы: {zone}")

    if len(selected_dates) != 2:
        st.info("Выбери начало и конец периода.")
        return

    start, end = selected_dates
    observed = period_filter(
        station, "local_time", start, end
    )
    if observed.empty:
        st.info("За выбранный период наблюдений нет.")
        return

    forecasts = None
    if station_predictions is not None:
        forecasts = period_filter(
            station_predictions, "target_local", start, end
        )
        actual = observed[
            ["timestamp_utc", "occupancy_pct"]
        ].rename(columns={
            "timestamp_utc": "target_timestamp_utc",
            "occupancy_pct": "mart_actual",
        })
        checked = forecasts.merge(
            actual,
            on="target_timestamp_utc",
            how="left",
            validate="one_to_one",
        )
        difference = (
            checked["actual_occupancy_pct"]
            - checked["mart_actual"]
        ).abs()
        if (
            checked["mart_actual"].isna().any()
            or not difference.le(1e-6).all()
        ):
            forecast_problem = (
                "Наблюдения обновились; прогнозы требуют "
                "повторной проверки."
            )
            forecasts = None

    st.subheader(names[code])
    capacities = observed["capacity"].dropna().unique()
    capacity = (
        str(int(capacities[0]))
        if len(capacities) == 1 else "Разная"
    )

    cards = st.columns(4)
    cards[0].metric("Справочная вместимость", capacity)
    cards[1].metric(
        "Средняя загрузка",
        f"{observed['occupancy_pct'].mean():.2f}%",
    )
    cards[2].metric(
        "Максимальная загрузка",
        f"{observed['occupancy_pct'].max():.2f}%",
    )
    cards[3].metric(
        "Наблюдений",
        f"{len(observed):,}".replace(",", " "),
    )
    st.caption(
        f"Наблюдения: "
        f"{observed['local_time'].min():%d.%m.%Y %H:%M} — "
        f"{observed['local_time'].max():%d.%m.%Y %H:%M}. "
        "Средние рассчитаны по имеющимся записям."
    )
    if observed["capacity_history_verified"].eq("false").any():
        st.caption(
            "Проценты рассчитаны по справочной вместимости. "
            "Её неизменность на всём историческом периоде "
            "не подтверждена."
        )

    figure = go.Figure()
    figure.add_trace(line_trace(
        observed, "timestamp_utc", "occupancy_pct",
        "Фактическая загрузка", "#111827", interval, zone,
    ))

    available = forecasts is not None and not forecasts.empty
    selected_model = (
        test_report["stations"][code]["selected_model_by_validation"]
        if available else None
    )

    if available:
        figure.add_trace(line_trace(
            forecasts,
            "target_timestamp_utc",
            "selected_prediction_pct",
            f"Прогноз: {LABELS[selected_model]}",
            "#2563eb", interval, zone,
        ))
        if show_baseline and selected_model != "persistence":
            figure.add_trace(line_trace(
                forecasts, "target_timestamp_utc",
                "persistence_pct", "Простой прогноз",
                "#ea580c", interval, zone, "dash",
            ))
        if show_both:
            for method in ("linear_regression", "random_forest"):
                if method != selected_model:
                    figure.add_trace(line_trace(
                        forecasts, "target_timestamp_utc",
                        method + "_pct", LABELS[method],
                        "#16a34a", interval, zone, "dot",
                    ))

    figure = style_chart(
        figure, "Загрузка, %", height=440
    )
    figure.update_yaxes(range=[0, 105])
    figure.update_xaxes(
        title=(
            "Время наблюдения или целевое время "
            f"прогноза ({zone})"
        )
    )
    st.plotly_chart(figure, width="stretch")

    if forecast_problem:
        st.warning(
            "Прогнозы временно недоступны: " + forecast_problem
        )
    elif not available:
        st.info(
            "В выбранном периоде нет сохранённых тестовых "
            "прогнозов. Измени период, чтобы посмотреть их."
        )

    if available:
        st.subheader("Качество исторических прогнозов")
        st.caption(
            f"Модель выбрана по валидации: "
            f"{LABELS[selected_model]}. "
            f"Оценено прогнозов: {len(forecasts)}. "
            "Ошибки рассчитаны только по сопоставленным "
            "примерам выбранного периода."
        )
        errors = (
            forecasts["selected_prediction_pct"]
            - forecasts["actual_occupancy_pct"]
        )
        mae = float(errors.abs().mean())
        rmse = math.sqrt(float((errors ** 2).mean()))
        baseline = float((
            forecasts["persistence_pct"]
            - forecasts["actual_occupancy_pct"]
        ).abs().mean())

        scores = st.columns(3)
        scores[0].metric("MAE модели", f"{mae:.4f} п.п.")
        scores[1].metric("RMSE модели", f"{rmse:.4f} п.п.")
        scores[2].metric(
            "MAE простого прогноза", f"{baseline:.4f} п.п."
        )

        if baseline > 0:
            change = 100 * (baseline - mae) / baseline
            if change >= 0:
                st.success(
                    "MAE модели ниже ошибки простого прогноза "
                    f"на {change:.2f}% за выбранный период."
                )
            else:
                st.warning(
                    "MAE модели выше ошибки простого прогноза "
                    f"на {-change:.2f}% за выбранный период."
                )

    left, right = st.columns(2)
    with left:
        st.subheader("Загрузка по часам")
        profile = observed.groupby(
            observed["local_time"].dt.hour
        )["occupancy_pct"].mean().reindex(range(24))

        chart = go.Figure(go.Bar(
            x=list(range(24)),
            y=profile.tolist(),
            marker_color="#2563eb",
        ))
        chart = style_chart(
            chart, "Средняя загрузка, %", 300
        )
        chart.update_xaxes(title="Час", dtick=2)
        chart.update_yaxes(range=[0, 105])
        st.plotly_chart(chart, width="stretch")

    with right:
        st.subheader("Загрузка по дням недели")
        profile = observed.groupby(
            observed["local_time"].dt.dayofweek
        )["occupancy_pct"].mean().reindex(range(7))

        chart = go.Figure(go.Bar(
            x=WEEKDAYS,
            y=profile.tolist(),
            marker_color="#16a34a",
        ))
        chart = style_chart(
            chart, "Средняя загрузка, %", 300
        )
        chart.update_yaxes(range=[0, 105])
        st.plotly_chart(chart, width="stretch")

    weather_columns = {
        "weather_status", "weather_timestamp_utc",
        "temperature_c", "precipitation_mm",
    }
    if weather_columns.issubset(observed.columns):
        with st.expander("Погода за выбранный период"):
            weather = observed[
                observed["weather_status"] == "matched"
            ].drop_duplicates("weather_timestamp_utc").copy()

            if weather.empty:
                st.info("Сопоставленная погода отсутствует.")
            else:
                weather["time"] = pd.to_datetime(
                    weather["weather_timestamp_utc"],
                    utc=True,
                    format="ISO8601",
                )
                weather = weather.sort_values("time")
                x = (
                    weather["time"].dt.tz_convert(zone)
                    .dt.tz_localize(None)
                )

                chart = go.Figure()
                chart.add_trace(go.Scatter(
                    x=x,
                    y=weather["temperature_c"],
                    name="Температура, °C",
                    mode="lines",
                    line={"color": "#ea580c"},
                ))
                chart.add_trace(go.Bar(
                    x=x,
                    y=weather["precipitation_mm"],
                    name="Осадки, мм",
                    yaxis="y2",
                    marker_color="#2563eb",
                    opacity=0.5,
                ))
                chart = style_chart(
                    chart, "Температура, °C", 300
                )
                chart.update_layout(yaxis2={
                    "title": "Осадки, мм",
                    "overlaying": "y",
                    "side": "right",
                    "rangemode": "tozero",
                })
                st.plotly_chart(chart, width="stretch")
                st.caption(
                    "Погода показана для контекста. "
                    "В обученных моделях погодные "
                    "признаки отключены."
                )

    with st.expander("Наблюдения и выгрузка"):
        table = observed[[
            "local_time", "occupied",
            "capacity", "occupancy_pct",
        ]].copy()
        table["local_time"] = (
            table["local_time"]
            .dt.strftime("%d.%m.%Y %H:%M %z")
        )
        table = table.rename(columns={
            "local_time": "Время Europe/Rome",
            "occupied": "Занято мест",
            "capacity": "Справочная вместимость",
            "occupancy_pct": "Загрузка, %",
        })
        st.dataframe(
            table, hide_index=True, width="stretch"
        )

        export = observed.drop(columns=["local_time"])
        st.download_button(
            "Скачать наблюдения (CSV)",
            data=export.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"parking_{code}_{start}_{end}.csv",
            mime="text/csv",
        )
        if available:
            st.download_button(
                "Скачать прогнозы (CSV)",
                data=forecasts.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"forecasts_{code}_{start}_{end}.csv",
                mime="text/csv",
            )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError) as error:
        st.error(
            f"Не удалось открыть данные дашборда: {error}"
        )