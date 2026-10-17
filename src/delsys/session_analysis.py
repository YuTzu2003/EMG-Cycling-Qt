"""Versioned, device-independent cycling session analysis and HTML export."""
from __future__ import annotations

import html
import json
import math
from bisect import bisect_left
from collections import defaultdict, deque
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


def rms_envelope(rows: list[dict], window_s: float = 1.0) -> list[tuple[float, float]]:
    """Return a trailing moving-average envelope for recorded RMS points.

    RMS values are already produced from filtered EMG by the acquisition path.  This
    deliberately smooths those feature values (rather than the raw waveform) so an
    archived ``.analysis.json`` has everything needed to reproduce the chart.
    """
    window_s = finite(window_s)
    if window_s is None or window_s <= 0:
        raise ValueError("window_s must be a positive finite number")
    samples = sorted(
        ((time_s, value) for row in rows
         if (time_s := finite(row.get("time_s"))) is not None
         and (value := finite(row.get("rms_mv"))) is not None),
        key=lambda point: point[0],
    )
    window: deque[tuple[float, float]] = deque()
    total = 0.0
    envelope = []
    for time_s, value in samples:
        window.append((time_s, value))
        total += value
        while window and window[0][0] < time_s - window_s:
            _old_time, old_value = window.popleft()
            total -= old_value
        envelope.append((time_s, total / len(window)))
    return envelope


def peak_envelope(rows: list[dict], window_s: float = 1.0) -> list[tuple[float, float]]:
    """Return one highest recorded RMS value for each consecutive time window.

    This is a peak envelope of the archived RMS feature: it does not recreate a
    peak envelope directly from the raw EMG waveform.  Empty time windows are
    omitted rather than converted into zero-valued muscle activity.
    """
    window_s = finite(window_s)
    if window_s is None or window_s <= 0:
        raise ValueError("window_s must be a positive finite number")
    samples = sorted(
        ((time_s, value) for row in rows
         if (time_s := finite(row.get("time_s"))) is not None
         and (value := finite(row.get("rms_mv"))) is not None),
        key=lambda point: point[0],
    )
    if not samples:
        return []
    window_start = samples[0][0]
    peak_time, peak_value = samples[0]
    envelope = []
    for time_s, value in samples[1:]:
        while time_s >= window_start + window_s:
            envelope.append((peak_time, peak_value))
            window_start += window_s
            peak_time, peak_value = time_s, value
        if value > peak_value:
            peak_time, peak_value = time_s, value
    envelope.append((peak_time, peak_value))
    return envelope


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
    # EMG and trainer timestamps now retain their recorded timing.  Older
    # sessions can contain a manual offset, but it is intentionally ignored:
    # the interface no longer offers manual cross-device time alignment.
    aligned_emg = streams["emg"]
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
    return {"duration_s": duration, "emg_trainer_offset_s": 0.0, "emg": emg_summary, "pedal": pedal,
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
    """Write a fixed report for the current session's EMG summary only.

    Pedal, trainer, resistance, and historical-comparison output are deliberately
    excluded: they make the current-session EMG report hard to scan.  The
    reference_session and ftp_w arguments remain for compatible callers.
    """
    result = analyze(session, normalized)
    subject = session.get("subject", {})
    channels = session.get("channels", {})
    comparison_label = "平均 normalized RMS" if normalized else "平均 RMS－基準值"
    table_rows = []
    charts = []
    for slot in sorted(result["emg"], key=lambda value: int(value) if str(value).isdigit() else str(value)):
        data = result["emg"][slot]
        channel = channels.get(slot, {})
        name = channel.get("label") or channel.get("mapping_name") or "未命名肌肉"
        slot_label = f"S{int(slot):02d}" if str(slot).isdigit() else f"S{slot}"
        table_rows.append(
            "<tr>"
            f"<th scope=\"row\">{html.escape(slot_label)} {html.escape(str(name))}</th>"
            f"<td>{_fmt(data['rms_mv']['mean'], ' mV', 3)}</td>"
            f"<td>{_fmt(data['rms_mv']['max'], ' mV', 3)}</td>"
            f"<td>{_fmt(data['comparison']['mean'], '' if normalized else ' mV', 3)}</td>"
            f"<td>{_fmt(data['early_rms_mv'], ' mV', 3)} → {_fmt(data['late_rms_mv'], ' mV', 3)}</td>"
            "</tr>"
        )
        rows = [row for row in session.get("emg", []) if str(row.get("slot")) == str(slot)]
        envelope_rows = [{"time_s": time_s, "rms_mv": value}
                         for time_s, value in peak_envelope(rows)]
        charts.append(
            "<details class=\"muscle-chart\">"
            f"<summary>{html.escape(slot_label)} {html.escape(str(name))}：Peak RMS 包絡線</summary>"
            f"{_chart_fragment(f'{slot_label} {name} Peak RMS 包絡線', envelope_rows, 'rms_mv', '#6d4bc3')}"
            "</details>"
        )

    raw_files = ", ".join(str(name) for name in session.get("raw_files", {}).values()) or "未提供"
    metadata = "".join((
        f"<div><span>受試者</span><strong>{html.escape(str(subject.get('patientId', '未提供')))}</strong></div>",
        f"<div><span>測試者</span><strong>{html.escape(str(subject.get('name', '未提供')))}</strong></div>",
        f"<div><span>開始時間</span><strong>{html.escape(str(session.get('started_at', '--')))}</strong></div>",
        f"<div><span>EMG 取樣率</span><strong>{_fmt(session.get('sample_rate_hz'), ' Hz')}</strong></div>",
        f"<div><span>分析方法</span><strong>{html.escape(str(session.get('feature', '未提供')))}</strong></div>",
        f"<div><span>原始檔</span><strong>{html.escape(raw_files)}</strong></div>",
    ))
    if table_rows:
        analysis = (
            "<section><h2>肌電摘要</h2>"
            "<p class=\"hint\">數值為本次騎乘的 RMS 分析結果；基準比較僅在相容基準存在時顯示。</p>"
            "<div class=\"table-wrap\"><table><thead><tr>"
            f"<th>肌肉／通道</th><th>平均 RMS</th><th>Peak RMS</th><th>{comparison_label}</th>"
            "<th>前段 → 後段</th>"
            "</tr></thead><tbody>" + "".join(table_rows) + "</tbody></table></div></section>"
            "<section><h2>各肌肉 Peak RMS 包絡線</h2>"
            "<p class=\"hint\">每 1 秒取一次最高 RMS；展開單一肌肉即可查看其隨騎乘時間的變化。</p>" + "".join(charts) + "</section>"
        )
    else:
        analysis = (
            "<section class=\"empty\"><h2>尚無可匯出的本次肌電資料</h2>"
            "<p>請先匯入或完成本次 EMG 紀錄，再重新匯出報表。</p></section>"
        )
    content = (
        "<header><p class=\"eyebrow\">EMG CYCLING MONITOR</p><h1>本次肌電分析報表</h1>"
        "<p class=\"subtitle\">固定格式｜僅包含本次摘要的 EMG 分析</p></header>"
        f"<section><h2>紀錄資訊</h2><div class=\"metadata\">{metadata}</div></section>"
        + analysis
        + "<footer>研究用途：本報表為訊號分析摘要，不作醫療診斷。</footer>"
    )
    document = (
        '<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>本次肌電分析報表</title><style>'
        ':root{color-scheme:light;font-family:"Segoe UI","Microsoft JhengHei",sans-serif;color:#172033;background:#f4f7fa}'
        'body{max-width:1120px;margin:0 auto;padding:36px 24px 48px;line-height:1.55}'
        'header{border-left:5px solid #1684d7;padding:4px 0 4px 18px;margin-bottom:24px}'
        'h1,h2{margin:0;color:#13243a}h1{font-size:28px}.eyebrow{margin:0;color:#1684d7;font-size:12px;font-weight:700;letter-spacing:.12em}.subtitle,.hint{color:#617086}.subtitle{margin:5px 0 0}'
        'section{background:#fff;border:1px solid #dbe3ec;border-radius:10px;padding:20px;margin:16px 0;box-shadow:0 1px 2px #13243a0a}'
        'h2{font-size:18px;margin-bottom:12px}.metadata{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 20px}.metadata div{min-width:0}.metadata span{display:block;color:#617086;font-size:13px}.metadata strong{display:block;overflow-wrap:anywhere;font-weight:500}'
        '.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:740px}th,td{padding:10px 12px;border-bottom:1px solid #e5ebf1;text-align:left;vertical-align:top}thead th{background:#eff6fc;color:#25415f;font-size:13px;white-space:nowrap}tbody th{font-weight:600}.muscle-chart{border-top:1px solid #e5ebf1;padding:12px 0}.muscle-chart:last-child{padding-bottom:0}.muscle-chart summary{cursor:pointer;color:#164b7a;font-weight:600}.muscle-chart section{box-shadow:none;border:0;padding:12px 0 0;margin:0}.muscle-chart h2{font-size:15px}.muscle-chart svg{width:100%;background:#fbfcfe;border:1px solid #e5ebf1}.empty{text-align:center;padding:40px 20px}footer{margin-top:24px;color:#617086;font-size:13px;text-align:center}@media(max-width:680px){body{padding:24px 14px}.metadata{grid-template-columns:1fr}section{padding:16px}}'
        '</style></head><body>' + content + '</body></html>'
    )
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
