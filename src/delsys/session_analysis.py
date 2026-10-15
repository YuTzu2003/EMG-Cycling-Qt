"""Versioned, device-independent cycling session analysis and HTML export."""
from __future__ import annotations

import html
import json
import math
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

VERSION = 1


def finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def compare_rms(current_mv, baseline_mv):
    """Signed baseline subtraction and optional ratio; never divide by zero."""
    current, baseline = finite(current_mv), finite(baseline_mv)
    if current is None:
        return {"delta_mv": None, "percent": None, "normalized": None, "quality": "EMG 資料不足"}
    if baseline is None:
        return {"delta_mv": None, "percent": None, "normalized": None, "quality": "無基準值"}
    if baseline <= 1e-9:
        return {"delta_mv": None, "percent": None, "normalized": None, "quality": "基準值過低"}
    delta = current - baseline
    return {"delta_mv": delta, "percent": 100 * delta / baseline,
            "normalized": current / baseline, "quality": "有效"}


def new_session(subject: dict, baselines: dict, sample_rate_hz: float | None = None) -> dict:
    return {"version": VERSION, "subject": subject, "baselines": baselines,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "sample_rate_hz": sample_rate_hz, "feature": "causal_filtered_200ms_sliding_rms_mv",
            "clock": "GUI monotonic receipt; approximate across devices",
            "emg_trainer_offset_s": 0.0,
            "emg": [], "pedal": [], "trainer": [], "raw_files": {}, "warnings": []}


def add_point(session: dict, stream: str, point: dict) -> None:
    if stream not in {"emg", "pedal", "trainer"}:
        raise ValueError("Unknown stream")
    t = finite(point.get("time_s"))
    if t is None or t < 0:
        return
    clean = {}
    for key, value in point.items():
        if value is None or isinstance(value, (str, bool)):
            clean[key] = value
        elif isinstance(value, int):
            clean[key] = value
        elif isinstance(value, float):
            clean[key] = finite(value)
    session[stream].append(clean)


def _stats(values):
    numbers = [number for value in values if (number := finite(value)) is not None]
    return {"mean": mean(numbers) if numbers else None,
            "max": max(numbers) if numbers else None, "count": len(numbers)}


def _pearson(pairs):
    values = [(finite(x), finite(y)) for x, y in pairs]
    values = [(x, y) for x, y in values if x is not None and y is not None]
    if len(values) < 10:
        return None
    mx, my = mean(x for x, _ in values), mean(y for _, y in values)
    top = sum((x - mx) * (y - my) for x, y in values)
    xx = sum((x - mx) ** 2 for x, _ in values)
    yy = sum((y - my) ** 2 for _, y in values)
    return top / math.sqrt(xx * yy) if xx > 0 and yy > 0 else None


def _nearest(rows: list[dict], times: list[float], time_s: float, max_gap_s: float = 2.0):
    if not rows:
        return None
    if time_s < times[0] - 0.5 or time_s > times[-1] + 0.5:
        return None
    index = bisect_left(times, time_s)
    candidates = rows[max(0, index - 1):index + 1]
    nearest = min(candidates, key=lambda row: abs(float(row["time_s"]) - time_s))
    return nearest if abs(float(nearest["time_s"]) - time_s) <= max_gap_s else None


def analyze(session: dict, normalized: bool = False) -> dict:
    """Align sparse streams to one-second bins, retaining missing values."""
    streams = {name: sorted(session.get(name, []), key=lambda row: row["time_s"])
               for name in ("emg", "pedal", "trainer")}
    offset = finite(session.get("emg_trainer_offset_s")) or 0.0
    aligned_emg = ([{**row, "time_s": float(row["time_s"]) + offset}
                    for row in streams["emg"] if float(row["time_s"]) + offset >= 0]
                   if offset else streams["emg"])
    aligned_streams = {"emg": aligned_emg, "trainer": streams["trainer"]}
    stream_times = {name: [float(row["time_s"]) for row in rows] for name, rows in aligned_streams.items()}
    emg_channels = defaultdict(list)
    for row in aligned_emg:
        emg_channels[str(row.get("slot", ""))].append(row)
    emg_times = {slot: [float(row["time_s"]) for row in rows] for slot, rows in emg_channels.items()}
    all_points = [row for rows in streams.values() for row in rows]
    duration = max((float(row["time_s"]) for row in all_points), default=0.0)
    alignment_duration = max(duration, max((float(row["time_s"]) for row in aligned_emg), default=0.0))
    seconds = range(min(int(alignment_duration) + 1, 7201))
    aligned = []
    for second in seconds:
        row = {"time_s": second}
        for name in ("emg", "trainer"):
            rows = aligned_streams[name]
            match = _nearest(rows, stream_times[name], second)
            row[name] = match if match else None
        row["emg_channels"] = {slot: match for slot, rows in emg_channels.items()
                               if (match := _nearest(rows, emg_times[slot], second)) is not None}
        aligned.append(row)

    emg_by_slot = defaultdict(list)
    invalid_by_slot = defaultdict(set)
    for row in streams["emg"]:
        slot = str(row.get("slot", ""))
        if finite(row.get("rms_mv")) is not None:
            emg_by_slot[slot].append(row)
        if row.get("quality") != "有效":
            invalid_by_slot[slot].add(str(row.get("quality", "資料不足")))
    emg_summary = {}
    for slot in sorted(set(emg_by_slot) | set(invalid_by_slot)):
        rows = emg_by_slot[slot]
        key = "normalized" if normalized else "delta_mv"
        cutoff = duration / 3
        early = _stats(row.get("rms_mv") for row in rows if row["time_s"] <= cutoff)
        late = _stats(row.get("rms_mv") for row in rows if row["time_s"] >= duration - cutoff)
        emg_summary[slot] = {"rms_mv": _stats(row.get("rms_mv") for row in rows),
                             "comparison": _stats(row.get(key) for row in rows),
                             "early_rms_mv": early["mean"], "late_rms_mv": late["mean"],
                             "quality": sorted(invalid_by_slot[slot])}
    pedal = {key: _stats(row.get(key) for row in streams["pedal"])
             for key in ("power_w", "cadence_rpm", "left_balance_percent", "left_power_w", "right_power_w", "crank_revolutions")}
    crank = [finite(row.get("crank_revolutions")) for row in streams["pedal"]]
    crank = [value for value in crank if value is not None]
    pedal["crank_turns"] = max(0, crank[-1] - crank[0]) if len(crank) >= 2 else None
    trainer = {key: _stats(row.get(key) for row in streams["trainer"])
               for key in ("resistance_level", "resistance_native_commanded", "resistance_native_reported", "power_watts", "speed_kph", "cadence_rpm", "heart_rate_bpm", "session_distance_meters", "energy_kcal")}
    resistance_segments = defaultdict(list)
    overlap = (bool(aligned_emg and streams["trainer"]) and
               max(aligned_emg[0]["time_s"], streams["trainer"][0]["time_s"]) <=
               min(aligned_emg[-1]["time_s"], streams["trainer"][-1]["time_s"]))
    for row in aligned:
        trainer_row = row["trainer"]
        level = finite(trainer_row.get("resistance_native_reported")) if trainer_row else None
        if overlap and level is not None and row["emg_channels"]:
            resistance_segments[level].append(row)
    segments = []
    for level, rows in sorted(resistance_segments.items()):
        muscle = {slot: _stats(point.get("rms_mv") for row in rows
                               if (point := row["emg_channels"].get(slot)) is not None)
                  for slot in emg_channels}
        segments.append({"resistance": level, "seconds": len(rows),
                         "power_w": _stats(row["trainer"].get("power_watts") for row in rows if row["trainer"])["mean"],
                         "cadence_rpm": _stats(row["trainer"].get("cadence_rpm") for row in rows if row["trainer"])["mean"],
                         "emg": muscle})
    load_relationship = {
        slot: _pearson((point.get("rms_mv"), row["trainer"].get("resistance_native_reported"))
                       for row in aligned if row["trainer"] is not None
                       if (point := row["emg_channels"].get(slot)) is not None)
        for slot in emg_channels
    }
    warnings = list(session.get("warnings", []))
    if not streams["emg"]:
        warnings.append("沒有 EMG 資料")
    if not streams["pedal"]:
        warnings.append("沒有踏板資料")
    if not streams["trainer"]:
        warnings.append("沒有阻力計資料")
    if streams["emg"] and streams["trainer"] and (not overlap or not any(
        row["emg"] is not None and row["trainer"] is not None for row in aligned
    )):
        warnings.append("EMG 與阻力計沒有可對齊的重疊時間")
    for slot, problems in invalid_by_slot.items():
        warnings.extend(f"S{int(slot):02d}：{problem}" for problem in sorted(problems) if slot.isdigit())
    return {"duration_s": duration, "emg_trainer_offset_s": offset, "emg": emg_summary, "pedal": pedal,
            "trainer": trainer, "resistance_segments": segments, "load_relationship": load_relationship,
            "aligned": aligned,
            "warnings": sorted(set(warnings))}


def save_session(path: Path, session: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(session, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def load_session(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != VERSION:
        raise ValueError("不支援的騎乘分析檔版本")
    for name in ("emg", "pedal", "trainer"):
        if not isinstance(data.get(name), list):
            raise ValueError(f"分析檔缺少 {name} 資料")
        if any(not isinstance(row, dict) or finite(row.get("time_s")) is None or
               float(row["time_s"]) < 0 for row in data[name]):
            raise ValueError(f"分析檔的 {name} 時間資料無效")
    return data


def _fmt(value, unit="", digits=1):
    number = finite(value)
    return "--" if number is None else f"{number:.{digits}f}{unit}"


def summary_lines(result: dict, normalized: bool = False) -> list[str]:
    lines = [f"騎乘時間：{_fmt(result['duration_s'], ' 秒', 0)}",
             f"EMG 相對阻力時間偏移：{result['emg_trainer_offset_s']:+.2f} 秒（手動調整；非硬體同步）",
             "EMG 比較：RMS / baseline" if normalized else "EMG 比較：RMS - baseline（mV）", ""]
    for slot, data in result["emg"].items():
        comparison = data["comparison"]
        lines.append(f"S{int(slot):02d} 平均 RMS {_fmt(data['rms_mv']['mean'], ' mV', 3)}；最大 {_fmt(data['rms_mv']['max'], ' mV', 3)}；平均比較 {_fmt(comparison['mean'], ' 倍' if normalized else ' mV', 3)}；前段 {_fmt(data['early_rms_mv'], ' mV', 3)} → 後段 {_fmt(data['late_rms_mv'], ' mV', 3)}")
    if not result["emg"]:
        lines.append("EMG：--")
    lines += ["", "踏板：平均功率 " + _fmt(result["pedal"]["power_w"]["mean"], " W") +
              "；平均踏頻 " + _fmt(result["pedal"]["cadence_rpm"]["mean"], " rpm") +
              "；平均左平衡 " + _fmt(result["pedal"]["left_balance_percent"]["mean"], "%"),
              "踏板：左／右平均功率 " + _fmt(result["pedal"]["left_power_w"]["mean"], " W") + " / " +
              _fmt(result["pedal"]["right_power_w"]["mean"], " W") + "；曲柄轉數變化 " + _fmt(result["pedal"]["crank_turns"], " 圈", 0),
              "阻力計：平均指令阻力 " + _fmt(result["trainer"]["resistance_level"]["mean"]) +
              "；原生指令 " + _fmt(result["trainer"]["resistance_native_commanded"]["mean"]) +
              "；平均回報阻力 " + _fmt(result["trainer"]["resistance_native_reported"]["mean"]) +
              "；最大回報阻力 " + _fmt(result["trainer"]["resistance_native_reported"]["max"]) +
              "；距離最大值 " + _fmt(result["trainer"]["session_distance_meters"]["max"], " m"),
              "阻力計：平均功率 " + _fmt(result["trainer"]["power_watts"]["mean"], " W") +
              "；速度 " + _fmt(result["trainer"]["speed_kph"]["mean"], " km/h") +
              "；踏頻 " + _fmt(result["trainer"]["cadence_rpm"]["mean"], " rpm") +
              "；心率 " + _fmt(result["trainer"]["heart_rate_bpm"]["mean"], " bpm") +
              "；消耗能量 " + _fmt(result["trainer"]["energy_kcal"]["max"], " kcal")]
    if result["resistance_segments"]:
        lines += ["", "阻力區段（近似同步，每秒取樣）："]
        for segment in result["resistance_segments"]:
            muscles = "、".join(f"S{int(slot):02d} {_fmt(stats['mean'], ' mV', 3)}"
                               for slot, stats in segment["emg"].items() if stats["count"])
            lines.append(f"阻力 {_fmt(segment['resistance'])}｜{segment['seconds']} 秒｜訓練台功率 {_fmt(segment['power_w'], ' W')}｜踏頻 {_fmt(segment['cadence_rpm'], ' rpm')}｜EMG {muscles or '--'}")
    relationships = [f"S{int(slot):02d} r={value:.2f}" for slot, value in result["load_relationship"].items() if value is not None]
    if relationships:
        lines += ["", "同時間 EMG RMS／回報阻力 Pearson r（僅描述共同變化）：" + "、".join(relationships)]
    if result["warnings"]:
        lines += ["", "資料品質：", *result["warnings"]]
    return lines


def export_html(path: Path, session: dict, normalized: bool = False, reference_session: dict | None = None,
                ftp_w: int = 200) -> None:
    """Write a standalone report with inline SVG and escaped session metadata."""
    result = analyze(session, normalized)
    lines = summary_lines(result, normalized)
    from delsys.pedal.analysis import pedal_analysis_lines
    pedal_rows = [{**row, "timestamp": row.get("time_s")} for row in session.get("pedal", [])]
    pedal_report = "\n".join(pedal_analysis_lines(pedal_rows, ftp_w)) if pedal_rows else "沒有踏板資料"
    emg_sections = []
    baseline_lines = []
    for slot, record in sorted(session.get("baselines", {}).items()):
        baseline_lines.append(f"S{slot}: {_fmt(record.get('rms_mv'), ' mV', 3)}；{record.get('source', '')}；{record.get('timestamp', '')}")
    for slot in sorted(result["emg"]):
        rows = [row for row in session["emg"] if str(row.get("slot")) == slot]
        emg_sections.append(_chart_fragment(f"S{int(slot):02d} EMG RMS", rows, "rms_mv", "#7e57c2"))
        emg_sections.append(_chart_fragment(f"S{int(slot):02d} {'normalized RMS' if normalized else 'RMS - baseline'}",
                                            rows, "normalized" if normalized else "delta_mv", "#c2185b"))
    content = "\n".join(("<h1>EMG Cycling 騎乘分析</h1>",
        f"<p>受試者：{html.escape(str(session.get('subject', {}).get('patientId', '')))}　測試者：{html.escape(str(session.get('subject', {}).get('name', '')))}</p>",
        f"<p>開始時間：{html.escape(str(session.get('started_at', '--')))}　EMG 取樣率：{_fmt(session.get('sample_rate_hz'), ' Hz')}</p>",
        f"<p>分析方法：{html.escape(str(session.get('feature', '')))}；原始檔：{html.escape(', '.join(str(name) for name in session.get('raw_files', {}).values()))}</p>",
        f"<p>基準快照：{html.escape(' / '.join(baseline_lines) or '無')}</p>",
        "<p>研究用途；跨設備時間以程式接收時間近似對齊，未使用共同硬體觸發。</p>",
        f"<pre>{html.escape(chr(10).join(lines))}</pre>", *emg_sections,
        f"<section><h2>踏板訓練指標</h2><pre>{html.escape(pedal_report)}</pre><p>依設定 FTP 計算；資料空窗不視為零功率。左右功率為估算值，非踩踏相位。</p></section>",
        _chart_fragment("踏板功率", session["pedal"], "power_w", "#c2185b"),
        _chart_fragment("踏板踏頻", session["pedal"], "cadence_rpm", "#f39c12"),
        _chart_fragment("踏板左平衡", session["pedal"], "left_balance_percent", "#43a047"),
        _chart_fragment("阻力回報值", session["trainer"], "resistance_native_reported", "#1684d7")))
    if reference_session is not None:
        from delsys.session_compare import compare_sessions, comparison_charts, comparison_html
        comparison = compare_sessions(reference_session, session)
        content += comparison_html(comparison) + comparison_charts(reference_session, session, comparison)
    document = '<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>騎乘分析</title><style>body{font:16px system-ui;max-width:1000px;margin:32px auto;color:#222}section{border:1px solid #ddd;padding:12px;margin:18px 0}svg{width:100%;background:#fafafa}pre{white-space:pre-wrap;line-height:1.7}table{border-collapse:collapse;width:100%}td,th{text-align:left;border-color:#ddd}</style>' + content + '</html>'
    path.write_text(document, encoding="utf-8")


def _chart_fragment(title, rows, key, color):
    pairs = [(finite(row.get("time_s")), finite(row.get(key))) for row in rows]
    pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
    if not pairs:
        return f"<section><h2>{html.escape(title)}</h2><p>無資料</p></section>"
    xmax = max(x for x, _ in pairs) or 1
    ymin, ymax = min(y for _, y in pairs), max(y for _, y in pairs)
    span = ymax - ymin or 1
    points = " ".join(f"{30 + 720*x/xmax:.1f},{170 - 130*(y-ymin)/span:.1f}" for x, y in pairs[::max(1, len(pairs)//1000)])
    return f'<section><h2>{html.escape(title)}</h2><svg viewBox="0 0 780 200" role="img" aria-label="{html.escape(title)}"><polyline fill="none" stroke="{color}" stroke-width="2" points="{points}"/></svg><p>{_fmt(ymin)}–{_fmt(ymax)}；0–{_fmt(xmax, " 秒")}</p></section>'
