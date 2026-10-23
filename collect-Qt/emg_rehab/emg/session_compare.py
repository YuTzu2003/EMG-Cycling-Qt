"""Descriptive comparison of two independent EMG cycling recordings."""
from __future__ import annotations

import html
import math
from statistics import median

from emg_rehab.emg.session_analysis import finite


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _channel_metadata(session: dict, slot: str) -> dict | None:
    metadata = session.get("channels", {}).get(slot)
    if metadata:
        return metadata
    baseline = session.get("baselines", {}).get(slot)
    if baseline:
        return {"mapping_name": baseline.get("mapping_name"),
                "sensor_serial": baseline.get("sensor_serial"), "label": baseline.get("mapping_name")}
    return None


def _metrics(session: dict, slot: str) -> dict:
    rows = [row for row in session.get("emg", []) if str(row.get("slot")) == slot]
    rms = [value for row in rows if (value := finite(row.get("rms_mv"))) is not None]
    med = median(rms) if rms else None
    p90 = _percentile(rms, 0.9)
    base = session.get("baselines", {}).get(slot)
    compatible = bool(base and any(row.get("quality") == "有效" for row in rows))
    baseline_mv = finite(base.get("rms_mv")) if compatible else None
    return {"count": len(rms), "missing_percent": 100 * (len(rows) - len(rms)) / len(rows) if rows else None,
            "median_mv": med, "p90_mv": p90,
            "variation": p90 / med if med and med > 1e-9 and p90 is not None else None,
            "baseline_adjusted_mv": med - baseline_mv if med is not None and baseline_mv and baseline_mv > 1e-9 else None,
            "normalized": med / baseline_mv if med is not None and baseline_mv and baseline_mv > 1e-9 else None}


def compare_sessions(first: dict, second: dict) -> dict:
    """Match by muscle mapping, side, sensor and processing settings."""
    slots = sorted({str(row.get("slot")) for row in first.get("emg", [])} |
                   {str(row.get("slot")) for row in second.get("emg", [])}, key=lambda x: int(x) if x.isdigit() else 999)
    entries = []
    for slot in slots:
        a, b = _channel_metadata(first, slot), _channel_metadata(second, slot)
        reason = None
        if a is None or b is None or not a.get("mapping_name") or not b.get("mapping_name"):
            reason = "缺少通道／肌肉對應資料"
        elif a["mapping_name"] != b["mapping_name"]:
            reason = "肌肉或左右側對應不同"
        elif a.get("sensor_serial") not in (None, "Unknown SID") and b.get("sensor_serial") not in (None, "Unknown SID") and a.get("sensor_serial") != b.get("sensor_serial"):
            reason = "感測器 SID 不同"
        elif first.get("filter") != second.get("filter"):
            reason = "EMG 濾波設定不同"
        elif finite(first.get("sample_rate_hz")) is None or finite(second.get("sample_rate_hz")) is None or not math.isclose(float(first["sample_rate_hz"]), float(second["sample_rate_hz"]), abs_tol=0.01, rel_tol=0):
            reason = "EMG 取樣率不同或缺失"
        first_metrics, second_metrics = _metrics(first, slot), _metrics(second, slot)
        x, y = first_metrics["median_mv"], second_metrics["median_mv"]
        if reason is None and (x is None or y is None):
            reason = "其中一次沒有有效 RMS"
        if reason is None and x <= 1e-9:
            reason = "第一次中位 RMS 過低"
        difference = y - x if reason is None else None
        percent = 100 * difference / x if difference is not None and x > 1e-9 else None
        entries.append({"slot": slot, "label": (b or a or {}).get("label") or f"S{slot}",
                        "mapping_name": (b or a or {}).get("mapping_name"),
                        "first": first_metrics, "second": second_metrics,
                        "median_difference_mv": difference, "median_change_percent": percent,
                        "quality": reason or "可比較"})
    notes = ["RMS 是電活動，不等同肌肉力量；跨次比較需維持電極位置、姿勢、阻力與動作流程。",
             "本專案 P715 不提供左右 crank 扭力波形，未計算 EMG-Resistance 的逐圈波形一致度。"]
    by_muscle = {}
    for entry in entries:
        mapping = entry.get("mapping_name") or ""
        abbreviation = mapping.rsplit("|", 1)[-1]
        if abbreviation.endswith(("-R", "-L")):
            by_muscle.setdefault(abbreviation[:-2], {})[abbreviation[-1]] = entry
    bilateral = []
    for muscle, sides in sorted(by_muscle.items()):
        if "R" not in sides or "L" not in sides:
            continue
        values = []
        for recording in ("first", "second"):
            right = sides["R"][recording]["median_mv"]
            left = sides["L"][recording]["median_mv"]
            denominator = (right + left) / 2 if right is not None and left is not None else 0
            values.append((right - left) / denominator * 100 if denominator > 1e-9 else None)
        bilateral.append({"muscle": muscle, "first_right_minus_left_percent": values[0],
                          "second_right_minus_left_percent": values[1]})
    if first.get("subject", {}).get("patientId") != second.get("subject", {}).get("patientId"):
        notes.append("兩次受試者 ID 不同，請確認檔案選擇。")
    return {"first_started_at": first.get("started_at"), "second_started_at": second.get("started_at"),
            "channels": entries, "bilateral": bilateral, "notes": notes}


def comparison_lines(result: dict) -> list[str]:
    lines = [f"第一次：{result['first_started_at'] or '--'}",
             f"第二次：{result['second_started_at'] or '--'}", "",
             "以相同肌肉／側別的中位 RMS 比較；差異 = 第二次 − 第一次。"]
    for entry in result["channels"]:
        slot = f"S{entry['slot']} {entry['label']}"
        if entry["quality"] != "可比較":
            lines.append(f"{slot}：{entry['quality']}")
            continue
        a, b = entry["first"], entry["second"]
        lines.append(f"{slot}：中位 {a['median_mv']:.3f} → {b['median_mv']:.3f} mV；差異 {entry['median_difference_mv']:+.3f} mV（{entry['median_change_percent']:+.1f}%）")
        lines.append(f"  P90 {a['p90_mv']:.3f} → {b['p90_mv']:.3f} mV；P90／中位 {a['variation']:.2f} → {b['variation']:.2f} 倍" if a["variation"] is not None and b["variation"] is not None else "  波動比值：資料不足")
        if a["normalized"] is not None and b["normalized"] is not None:
            lines.append(f"  各次對自身基準：{a['normalized']:.2f} → {b['normalized']:.2f} 倍")
    if result["bilateral"]:
        lines += ["", "同名肌肉左右中位 RMS 相對差（右 − 左）："]
        for pair in result["bilateral"]:
            a, b = pair["first_right_minus_left_percent"], pair["second_right_minus_left_percent"]
            lines.append(f"{pair['muscle']}：{a:+.1f}% → {b:+.1f}%" if a is not None and b is not None else f"{pair['muscle']}：資料不足")
    return lines + ["", *result["notes"]]


def comparison_html(result: dict) -> str:
    rows = []
    for entry in result["channels"]:
        a, b = entry["first"], entry["second"]
        def fmt(value, suffix=""):
            number = finite(value)
            return "--" if number is None else f"{number:+.2f}{suffix}" if suffix in (" mV", "%") else f"{number:.3f}{suffix}"
        cells = (entry["label"], fmt(a["median_mv"]), fmt(b["median_mv"]),
                 fmt(entry["median_difference_mv"], " mV"), fmt(entry["median_change_percent"], "%"),
                 fmt(a["p90_mv"]), fmt(b["p90_mv"]),
                 f"{fmt(a['variation'])} / {fmt(b['variation'])}",
                 f"{fmt(a['normalized'])} / {fmt(b['normalized'])}", entry["quality"])
        rows.append("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in cells) + "</tr>")
    bilateral_lines = [f"{item['muscle']}：第一次 {item['first_right_minus_left_percent']:+.1f}%；第二次 {item['second_right_minus_left_percent']:+.1f}%"
                       for item in result["bilateral"] if item["first_right_minus_left_percent"] is not None and item["second_right_minus_left_percent"] is not None]
    dates = f"第一次：{result['first_started_at'] or '--'}；第二次：{result['second_started_at'] or '--'}"
    return "<section><h2>兩次騎乘肌肉活化比較</h2><p>" + html.escape(dates) + "</p><p>中位與 P90 單位：mV；差異為第二次減第一次。波動比為 P90／中位 RMS；基準倍數為各次中位 RMS／該次有效基準。</p><table border='1' cellpadding='7'><thead><tr><th>肌肉</th><th>第一次中位</th><th>第二次中位</th><th>差異</th><th>變化</th><th>第一次 P90</th><th>第二次 P90</th><th>波動比 1 / 2</th><th>基準倍數 1 / 2</th><th>品質</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table><h3>同名肌肉左右相對活化（右 − 左）</h3><pre>" + html.escape("\n".join(bilateral_lines) or "資料不足") + "</pre><pre>" + html.escape("\n".join(result["notes"])) + "</pre></section>"


def comparison_charts(first: dict, second: dict, result: dict) -> str:
    """Independent elapsed-time overlays for each matched muscle."""
    sections = []
    for entry in result["channels"]:
        if entry["quality"] != "可比較":
            continue
        series = []
        for session in (first, second):
            values = [(finite(row.get("time_s")), finite(row.get("rms_mv")))
                      for row in session.get("emg", []) if str(row.get("slot")) == entry["slot"]]
            series.append([(x, y) for x, y in values if x is not None and y is not None])
        if not all(series):
            continue
        xmax = max(x for points in series for x, _ in points) or 1
        ymin = min(y for points in series for _, y in points)
        ymax = max(y for points in series for _, y in points)
        span = ymax - ymin or 1
        curves = []
        for points, color in zip(series, ("#616161", "#0f6cbd"), strict=True):
            stride = max(1, len(points) // 1000)
            coords = " ".join(f"{30 + 720*x/xmax:.1f},{170 - 130*(y-ymin)/span:.1f}" for x, y in points[::stride])
            curves.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{coords}"/>')
        sections.append(f'<section><h3>{html.escape(entry["label"])}：兩次 RMS 曲線</h3><p>灰色：第一次；藍色：第二次。橫軸為各自騎乘起點後秒數；縱軸 mV。</p><svg viewBox="0 0 780 200" role="img" aria-label="兩次 RMS 曲線比較">{"".join(curves)}</svg></section>')
    return "".join(sections)
