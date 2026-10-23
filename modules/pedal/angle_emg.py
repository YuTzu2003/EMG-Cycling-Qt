from __future__ import annotations
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
import numpy as np
import pandas as pd
from scipy.signal import butter, filtfilt, find_peaks, iirnotch, sosfiltfilt, welch
from scipy.stats import linregress, mannwhitneyu

MUSCLES = {"BF": "股二頭肌", "VM": "股內側肌", "RF": "股直肌", "VL": "股外側肌","TA": "脛前肌", "GAS": "腓腸肌", "SOL": "比目魚肌"}
EXPECTED_PEAK = {"BF": 140, "VM": 80, "RF": 30, "VL": 80, "TA": 330, "GAS": 120, "SOL": 110}
MUSCLE_KEYS = {"BF": "biceps", "VM": "vastus medialis", "RF": "rectus", "VL": "vastus lateralis","TA": "tibialis", "GAS": "gastroc", "SOL": "soleus"}


def _muscle_key(name: str) -> str | None:
    """Classify both human CSV headers and slot-prefixed collection headers.

    The collector writes names such as ``Vastus_medialis_R`` while imported
    historical files may use ``S03_R_Vastus medialis``.  Separators must not
    change the anatomical classification.
    """
    normalized = re.sub(r"[_-]+", " ", name.casefold())
    return next((key for key, token in MUSCLE_KEYS.items() if token in normalized), None)

@dataclass(frozen=True)
class AngleSettings:
    start: tuple[float, float] = (5.0, 65.0)
    end: tuple[float, float] = (240.0, 300.0)
    bins: int = 24
    percentile: float = 99.0
    scale: float | None = None 
    offset_s: float | None = 0.0 
    def validate(self) -> None:
        if not (0 <= self.start[0] < self.start[1] <= self.end[0] < self.end[1]):
            raise ValueError("前段和後段須依時間排序、不可重疊。")
        if not 8 <= self.bins <= 72 or not 50 <= self.percentile < 100:
            raise ValueError("角度區間或正規化百分位設定不正確。")
        if self.scale is not None and not 0.8 <= self.scale <= 1.2:
            raise ValueError("EMG 時間比例須介於 0.8 和 1.2。")

def _emg(path: Path):
    frame = pd.read_csv(path, encoding="utf-8-sig")
    if "time_seconds" not in frame or len(frame) < 2000:
        raise ValueError("EMG CSV 缺少 time_seconds 或資料不足。")
    names = [name for name in frame if name != "time_seconds" and pd.api.types.is_numeric_dtype(frame[name])]
    if not names:
        raise ValueError("EMG CSV 沒有數值通道。")
    t = frame.time_seconds.to_numpy(dtype=float)
    dt = np.diff(t)
    if not np.all(np.isfinite(t)) or np.any(dt <= 0):
        raise ValueError("EMG 時間欄位必須遞增。")
    fs = 1 / np.median(dt)
    if fs <= 1000:
        raise ValueError("EMG 取樣率不足，無法使用 20–450 Hz 濾波。")
    values = frame[names].to_numpy(dtype=float)
    if not np.all(np.isfinite(values)):
        raise ValueError("EMG 通道含缺漏或非數值資料。")
    return t, values, names, fs


def _angles(path: Path):
    frame = pd.read_csv(path, encoding="utf-8-sig")
    calibrated = {"t_session_s", "right_pedal_deg", "left_pedal_deg", "crank_revolutions", "pedaling"}
    if calibrated <= set(frame):
        angle = frame[list(calibrated)].apply(pd.to_numeric, errors="coerce").dropna().sort_values("t_session_s")
        if len(angle) < 6 or np.any(np.diff(angle.t_session_s.to_numpy(float)) <= 0):
            raise ValueError("校正角度檔資料不足或時間順序不正確。")
        return angle, True
    required = {"session_seconds", "crank_revolutions", "crank_event_time"}
    if not required <= set(frame):
        raise ValueError("請選取本專案 Pedal CSV，或含踏板角度欄位的校正 CSV。")
    frame = frame.sort_values("session_seconds").dropna(subset=["session_seconds", "crank_revolutions"])
    # One row per revolution counter. The BLE event clock is 1/1024 s,
    # wrapping at 65536; packet arrival time gives the session-time anchor.
    changed = frame.crank_revolutions.diff().fillna(0).between(1, 3)
    events = frame.loc[changed, ["session_seconds", "crank_revolutions", "crank_event_time"]].copy()
    if len(events) < 6:
        raise ValueError("踏板曲柄事件不足，無法建立有效踩踏圈。")
    event_t = events.session_seconds.to_numpy(dtype=float)
    clock = events.crank_event_time.to_numpy(dtype=float)
    if np.all(np.isfinite(clock)):
        gaps = (np.diff(clock) % 65536) / 1024
        plausible = (gaps > 0.25) & (gaps < 3)
        # Use event-clock intervals, anchored at first packet. Avoid BLE
        # notification jitter, but reject counter skips / implausible gaps.
        if plausible.mean() >= 0.8:
            event_t = event_t[0] + np.r_[0, np.cumsum(np.where(plausible, gaps, np.diff(event_t)))]
    rev = events.crank_revolutions.to_numpy(dtype=float)
    keep = np.r_[True, (np.diff(event_t) > 0.25) & (np.diff(event_t) < 3) & (np.diff(rev) == 1)]
    event_t, rev = event_t[keep], rev[keep]
    if len(event_t) < 6:
        raise ValueError("有效且連續的曲柄事件不足。")
    # Dense phase samples, one per 0.01 s, only inside uninterrupted cycles.
    times, revolutions = [], []
    for index in range(len(event_t) - 1):
        duration = event_t[index + 1] - event_t[index]
        if rev[index + 1] != rev[index] + 1 or not 0.3 <= duration <= 3:
            continue
        segment = np.arange(event_t[index], event_t[index + 1], .01)
        times.append(segment)
        revolutions.append(rev[index] + (segment - event_t[index]) / duration)
    if not times:
        raise ValueError("踏板沒有完整的連續踩踏圈。")
    t = np.concatenate(times)
    r = np.concatenate(revolutions)
    phase = (r % 1) * 360
    return pd.DataFrame({"t_session_s": t, "right_pedal_deg": phase,
                         "left_pedal_deg": (phase + 180) % 360,
                         "crank_revolutions": r, "pedaling": 1}), False


def _filter(values, fs):
    band = butter(4, [20, 450], fs=fs, btype="band", output="sos")
    notch_b, notch_a = iirnotch(60, 30, fs)
    low = butter(4, 6, fs=fs, btype="low", output="sos")
    filtered = np.column_stack([filtfilt(notch_b, notch_a, sosfiltfilt(band, values[:, i]))
                                for i in range(values.shape[1])])
    envelope = np.column_stack([sosfiltfilt(low, np.abs(filtered[:, i]))
                                for i in range(values.shape[1])])
    return filtered, envelope


def _events(angle):
    t = angle.t_session_s.to_numpy(float)
    rev = angle.crank_revolutions.to_numpy(float)
    valid = np.isfinite(rev) & (angle.pedaling.to_numpy(float) > 0)
    t, rev = t[valid], rev[valid]
    events = []
    # Work is bounded by the number of supplied rows. Missing revolutions do
    # not provide enough phase information to interpolate an event.
    for index in range(1, len(rev)):
        gap = rev[index] - rev[index - 1]
        if not 0 < gap <= 2:
            continue
        for target in range(int(np.ceil(rev[index - 1])), int(np.floor(rev[index])) + 1):
            if target > rev[index - 1]:
                events.append(np.interp(target, rev[index - 1:index + 1], t[index - 1:index + 1]))
    return np.asarray(events)


def _fit_clock(t, envelope, event_t, fs):
    period = float(np.median(np.diff(event_t)))
    candidates = []
    for index in range(envelope.shape[1]):
        trace = envelope[:, index]
        if np.std(trace) <= 0:
            continue
        peaks, _ = find_peaks(trace / np.std(trace), distance=max(1, int(.6 * period * fs)), prominence=.8)
        bursts = t[peaks]
        if len(bursts) < 12 or abs(len(bursts) - len(event_t)) > .25 * len(event_t):
            continue
        best = None
        for shift in range(-25, 26):
            bt = bursts[max(0, -shift):]
            et = event_t[max(0, shift):]
            count = min(len(bt), len(et))
            if count < 10:
                continue
            corr = np.corrcoef(np.diff(bt[:count]), np.diff(et[:count]))[0, 1]
            if np.isfinite(corr) and (best is None or corr > best[0]):
                best = (corr, bt[:count], et[:count])
        if best is None or best[0] < .4:
            continue
        corr, bt, et = best
        scale, intercept = np.polyfit(et, bt, 1)
        residual = bt - (scale * et + intercept)
        if .8 <= scale <= 1.2 and np.std(residual) < .15:
            candidates.append({"channel_index": index, "scale": float(scale),
                               "correlation": float(corr), "residual_std_s": float(np.std(residual)),
                               "matched": len(bt)})
    candidates.sort(key=lambda item: (-item["correlation"], item["residual_std_s"]))
    if not candidates:
        return None, []
    return float(np.median([item["scale"] for item in candidates[:4]])), candidates[:4]


def _circular_mean(degrees):
    return float(np.degrees(np.angle(np.mean(np.exp(1j * np.radians(degrees))))) % 360)


def _side_name(name: str, side: str | None) -> str:
    if side not in {"L", "R"}:
        return name
    if re.match(r"^(?:[LR]_)?S\d+(?:_[LR])?_", name, flags=re.IGNORECASE):
        return name
    return re.sub(r"_[RL]$", "", name, flags=re.IGNORECASE) + f"_{side}"


def analysis_summary_text(result: dict) -> str:
    """Plain-language, measurement-only overview for the GUI and export."""
    summary = result["summary"]
    settings, sync = result["settings"], result["sync"]
    increased = int((summary.rms_change_percent > 0).sum())
    largest = summary.loc[summary.rms_change_percent.abs().idxmax()]
    shifted = summary.loc[summary.peak_angle_shift.abs().idxmax()]
    return (
        f"分析區間：前段 {settings['start'][0]:g}–{settings['start'][1]:g} 秒，"
        f"後段 {settings['end'][0]:g}–{settings['end'][1]:g} 秒。\n"
        f"有效踩踏圈：{sync['valid_cycles']} 圈；共 {len(summary)} 個有前後段資料的通道。"
        f"前段／後段每通道約 {int(summary.n_start.median())}／{int(summary.n_end.median())} 圈。\n"
        f"RMS 在後段較高的通道：{increased}/{len(summary)}。最大變化："
        f"{largest.location_zh} {largest.rms_change_percent:+.1f}%（"
        f"{largest.rms_uv_start:.1f} → {largest.rms_uv_end:.1f} µV）。\n"
        f"最大峰值相位位移：{shifted.location_zh} {shifted.peak_angle_shift:+.0f}°"
        f"（前段 {shifted.peak_angle_start:.0f}° → 後段 {shifted.peak_angle_end:.0f}°）。\n"
        f"相位基準：{sync['angle_label']}；時鐘比例 {sync['scale']:.5f}，"
        f"起點偏移 {sync['offset_s']:+.3f} 秒。"
    )


def phase_intensities(result: dict, angle_deg: float) -> pd.DataFrame:
    """Return each analysed muscle's normalized EMG signal at a crank phase.

    ``mean_percent`` is normalized independently for every channel.  It makes
    the timing and relative intensity within that channel easy to compare, but
    it is not a measurement of mechanical force between muscles.
    """
    phase = float(angle_deg) % 360.0
    profiles, summary = result["profiles"], result["summary"]
    rows = []
    for entry in summary.itertuples():
        values = {"channel": entry.channel, "channel_label": entry.channel_label,
                  "location_zh": entry.location_zh, "side": entry.side,
                  "side_zh": entry.side_zh}
        for window in ("start", "end"):
            curve = profiles[(profiles.channel == entry.channel) & (profiles.phase == window)]
            x = curve.angle_bin_center.to_numpy(float)
            y = curve.mean_percent.to_numpy(float)
            # Repeat the circular profile on both sides so interpolation works
            # at the 0°/360° boundary too.
            values[f"{window}_percent"] = float(np.interp(
                phase, np.r_[x - 360, x, x + 360], np.r_[y, y, y],
            ))
        values["change_percentage_points"] = values["end_percent"] - values["start_percent"]
        rows.append(values)
    return pd.DataFrame(rows).sort_values(
        ["end_percent", "start_percent"], ascending=False, ignore_index=True,
    )


def analyze_angle_emg(emg_path: Path, pedal_path: Path, sides: dict[str, str],
                      settings: AngleSettings = AngleSettings()) -> dict:
    """Return dataframes and metadata; keep this call on a worker thread."""
    settings.validate()
    t, raw, names, fs = _emg(emg_path)
    angle, calibrated = _angles(pedal_path)
    filtered, envelope = _filter(raw, fs)
    events = _events(angle)
    if len(events) < 6:
        raise ValueError("有效踏板圈數不足。")
    estimated, clock_refs = _fit_clock(t, envelope, events, fs)
    if settings.scale is None and estimated is None:
        raise ValueError("無法可靠估計 EMG 與踏板的時鐘比例。請檢查同步，或輸入已量測的時間比例。")
    scale = settings.scale if settings.scale is not None else estimated
    sync_state = "自動校正" if settings.scale is None else "手動時間比例"
    angle_t = angle.t_session_s.to_numpy(float)
    period = float(np.median(np.diff(events)))
    unshifted_t = t / scale
    estimates = []
    for index, name in enumerate(names):
        muscle = _muscle_key(name)
        side = sides.get(name)
        if muscle is None or side not in {"L", "R"}:
            continue
        phase = angle.left_pedal_deg if side == "L" else angle.right_pedal_deg
        phase_unwrapped = np.unwrap(np.radians(phase.to_numpy(float)))
        degrees = np.degrees(np.interp(unshifted_t, angle_t, phase_unwrapped, left=np.nan, right=np.nan)) % 360
        keep = np.isfinite(degrees)
        if not np.any(keep):
            continue
        activity = np.maximum(envelope[keep, index] - np.median(envelope[keep, index]), 0)
        if activity.sum() <= 0:
            continue
        observed = float(np.degrees(np.angle(np.sum(activity * np.exp(1j * np.radians(degrees[keep]))))) % 360)
        estimates.append((EXPECTED_PEAK[muscle] - observed + 180) % 360 - 180)
    if estimates and calibrated:
        vector = np.mean(np.exp(1j * np.radians(estimates)))
        template_offset = {"mean_expected_minus_observed_deg": float(np.degrees(np.angle(vector))),
                           "concentration": float(abs(vector)),
                           "offset_s": float(-np.degrees(np.angle(vector)) / 360 * period),
                           "channels": len(estimates)}
    else:
        template_offset = {"mean_expected_minus_observed_deg": None, "concentration": 0.0,
                           "offset_s": None, "channels": 0}
    if settings.offset_s is None:
        if not calibrated or template_offset["offset_s"] is None or template_offset["concentration"] < .3:
            raise ValueError("自動起點偏移需要已校正角度與足夠一致的肌肉訊號。")
        offset = template_offset["offset_s"]
    else:
        offset = settings.offset_s
    session_t = unshifted_t + offset
    rev = np.interp(session_t, angle_t, angle.crank_revolutions, left=np.nan, right=np.nan)
    pedaling = np.interp(session_t, angle_t, angle.pedaling, left=0, right=0) > .5
    right_unwrapped = np.unwrap(np.radians(angle.right_pedal_deg.to_numpy(float)))
    left_unwrapped = np.unwrap(np.radians(angle.left_pedal_deg.to_numpy(float)))
    right = np.degrees(np.interp(session_t, angle_t, right_unwrapped, left=np.nan, right=np.nan)) % 360
    left = np.degrees(np.interp(session_t, angle_t, left_unwrapped, left=np.nan, right=np.nan)) % 360
    valid = np.isfinite(rev) & np.isfinite(right) & np.isfinite(left) & pedaling
    after = np.searchsorted(angle_t, session_t, side="right")
    before = np.clip(after - 1, 0, len(angle_t) - 1)
    after = np.clip(after, 0, len(angle_t) - 1)
    valid &= angle_t[after] - angle_t[before] <= .05
    if not valid.any():
        raise ValueError("EMG 與踏板資料沒有重疊的有效踩踏時段。")
    centers = (np.arange(settings.bins) + .5) * 360 / settings.bins
    rows = []
    norms = {}
    for index, name in enumerate(names):
        side = sides.get(name)
        if side not in {"L", "R"}:
            continue
        muscle = _muscle_key(name)
        side_zh = "左" if side == "L" else "右"
        location_zh = side_zh + MUSCLES[muscle] if muscle else side_zh
        angle_deg = left if side == "L" else right
        # Cycle IDs come from the same continuous revolution stream.
        cycle = np.floor(rev + (.5 if side == "L" else 0)).astype(float)
        reference = float(np.percentile(envelope[valid, index], settings.percentile))
        if not np.isfinite(reference) or reference <= 0:
            continue
        norms[name] = reference
        normalized = envelope[:, index] / reference * 100
        for cid in np.unique(cycle[valid]):
            mask = valid & (cycle == cid)
            positions = np.flatnonzero(mask)
            if len(positions) < .5 * fs or np.max(np.diff(positions)) > .05 * fs:
                continue
            first, last = positions[0], positions[-1]
            # Require almost a whole revolution; reject edge fragments and
            # missing phase bins even if the sample count looks large.
            phase = angle_deg[mask]
            bin_id = np.floor(phase / (360 / settings.bins)).astype(int) % settings.bins
            if len(np.unique(bin_id)) != settings.bins:
                continue
            profile = np.array([np.mean(normalized[mask][bin_id == k]) for k in range(settings.bins)])
            weight = np.maximum(profile - profile.min(), 0)
            peak = float(centers[int(np.argmax(profile))])
            activation_center = (float(np.degrees(np.angle(np.sum(weight * np.exp(1j * np.radians(centers))))) % 360)
                                 if weight.sum() else peak)
            segment = filtered[first:last + 1, index]
            frequency, power = welch(segment, fs, nperseg=min(1024, len(segment)))
            mdf = float(frequency[np.searchsorted(np.cumsum(power), power.sum() / 2)])
            match = re.match(r"(?:[LR]_)?S(\d+)(?:_[LR])?", name, flags=re.IGNORECASE)
            short = f"S{int(match.group(1)):02d}" if match else name
            rows.append({"channel": name, "channel_label": _side_name(name, side),
                         "slot": short, "muscle": muscle, "location_zh": location_zh,
                         "side": side, "side_zh": side_zh,
                         "cycle": int(cid),
                         "time_s": float(np.mean(session_t[mask])), "rms_uv": float(np.sqrt(np.mean(segment ** 2)) * 1e6),
                         "mdf_hz": mdf, "mean_percent": float(np.mean(profile)),
                         "peak_percent": float(np.max(profile)), "peak_angle_deg": peak,
                         "activation_center_deg": activation_center,
                         "active_fraction": float(np.mean(profile > profile.min() + .2 * np.ptp(profile))),
                         **{f"bin_{k}": profile[k] for k in range(settings.bins)}})
    if not rows:
        raise ValueError("沒有完整且有效的踩踏圈；請檢查踏板缺漏、同步與感測器側別。")
    cycles = pd.DataFrame(rows)
    cycles["phase"] = np.select([cycles.time_s.between(*settings.start, inclusive="left"),
                                   cycles.time_s.between(*settings.end, inclusive="both")],
                                  ["start", "end"], default="mid")
    summary, profiles = [], []
    bin_columns = [f"bin_{k}" for k in range(settings.bins)]
    for name in cycles.channel.unique():
        channel = cycles[cycles.channel == name]
        start = channel[channel.phase == "start"]
        end = channel[channel.phase == "end"]
        for phase, subset in (("all", channel), ("start", start), ("end", end)):
            for k, center in enumerate(centers):
                profiles.append({"channel": name, "channel_label": channel.channel_label.iloc[0],
                                 "muscle": channel.muscle.iloc[0], "location_zh": channel.location_zh.iloc[0],
                                 "side": channel.side.iloc[0], "side_zh": channel.side_zh.iloc[0], "phase": phase,
                                 "angle_bin_center": center, "mean_percent": float(subset[bin_columns[k]].mean()),
                                 "sd_percent": float(subset[bin_columns[k]].std())})
        if len(start) < 5 or len(end) < 5:
            continue
        a, b = start.rms_uv.mean(), end.rms_uv.mean()
        peak_a, peak_b = _circular_mean(start.peak_angle_deg), _circular_mean(end.peak_angle_deg)
        trend_rms = linregress(channel.time_s, channel.rms_uv) if len(channel) > 2 else None
        trend_mdf = linregress(channel.time_s, channel.mdf_hz) if len(channel) > 2 else None
        summary.append({"channel": name, "channel_label": channel.channel_label.iloc[0],
                        "muscle": channel.muscle.iloc[0], "location_zh": channel.location_zh.iloc[0],
                        "side": channel.side.iloc[0], "side_zh": channel.side_zh.iloc[0],
                        "n_start": len(start), "n_end": len(end),
                        "norm_uv": norms[name] * 1e6, "rms_uv_start": a, "rms_uv_end": b,
                        "rms_change_percent": (b / a - 1) * 100 if a else np.nan,
                        "mean_percent_start": start.mean_percent.mean(), "mean_percent_end": end.mean_percent.mean(),
                        "peak_percent_start": start.peak_percent.mean(), "peak_percent_end": end.peak_percent.mean(),
                        "peak_angle_start": peak_a, "peak_angle_end": peak_b,
                        "peak_angle_shift": (peak_b - peak_a + 180) % 360 - 180,
                        "active_fraction_start": start.active_fraction.mean(), "active_fraction_end": end.active_fraction.mean(),
                        "mdf_start": start.mdf_hz.mean(), "mdf_end": end.mdf_hz.mean(),
                        "mdf_change_hz": end.mdf_hz.mean() - start.mdf_hz.mean(),
                        "p_mean": mannwhitneyu(start.mean_percent, end.mean_percent).pvalue,
                        "p_mdf": mannwhitneyu(start.mdf_hz, end.mdf_hz).pvalue,
                        "rms_slope_percent_per_min": trend_rms.slope * 60 / channel.rms_uv.mean() * 100 if trend_rms else np.nan,
                        "rms_slope_p": trend_rms.pvalue if trend_rms else np.nan,
                        "mdf_slope_hz_per_min": trend_mdf.slope * 60 if trend_mdf else np.nan,
                        "mdf_slope_p": trend_mdf.pvalue if trend_mdf else np.nan})
    if not summary:
        raise ValueError("前段或後段少於 5 個有效圈，請調整分析時間區間。")
    result = {"cycles": cycles, "profiles": pd.DataFrame(profiles), "summary": pd.DataFrame(summary),
            "settings": asdict(settings), "sync": {"state": sync_state, "scale": scale,
            "auto_scale": estimated, "scale_used": scale, "offset_s": offset,
            "offset_used_s": offset, "template_offset": template_offset,
            "channel_names": {name: _side_name(name, sides.get(name)) for name in names},
            "channel_locations_zh": dict(zip(cycles.channel, cycles.location_zh)),
            "leg_assignment": {_side_name(name, sides[name]): sides[name] for name in names if name in sides},
            "references": clock_refs,
            "angle_calibrated": calibrated, "angle_label": "踏板角度" if calibrated else "相對踩踏相位",
            "valid_cycles": int(cycles.cycle.nunique()), "fs_file_hz": fs, "fs_corrected_hz": fs * scale,
            "emg_file": str(emg_path), "pedal_file": str(pedal_path)}}
    result["overview"] = analysis_summary_text(result)
    return result


def export_angle_emg(result: dict, directory: Path, include_raw: bool = True,
                     envelope_rate: float = 100.0) -> None:
    """Write teacher-compatible synchronized data, tables and figures."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    installed = {font.name for font in font_manager.fontManager.ttflist}
    plt.rcParams["font.family"] = [name for name in ("Microsoft JhengHei", "Noto Sans CJK TC", "DejaVu Sans")
                                   if name in installed]
    plt.rcParams["axes.unicode_minus"] = False

    directory.mkdir(parents=True, exist_ok=True)
    cycles, profiles, summary = (result[key] for key in ("cycles", "profiles", "summary"))
    cycles.to_csv(directory / "emg_per_cycle.csv", index=False, encoding="utf-8-sig")
    profiles.to_csv(directory / "emg_angle_profiles.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(directory / "emg_start_vs_end_summary.csv", index=False, encoding="utf-8-sig")
    (directory / "angle_analysis_settings.json").write_text(json.dumps({"settings": result["settings"], "sync": result["sync"]},
                                                               ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "sync_report.json").write_text(json.dumps(result["sync"], ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "analysis_overview.txt").write_text(
        "EMG 踩踏角度分析摘要\n" + "=" * 24 + "\n" + result["overview"] + "\n\n"
        "判讀提醒：RMS 是肌電訊號強度；百分比為各通道相對自身參考值，"
        "不可直接比較不同肌肉的出力，也不作疲勞診斷。\n",
        encoding="utf-8",
    )
    channels = list(summary.channel)
    display = result["sync"]["channel_names"]
    locations = dict(zip(summary.channel, summary.location_zh, strict=True))
    def plot_name(channel: str) -> str:
        return f"{locations[channel]} · {display[channel]}"
    label = result["sync"]["angle_label"]
    fig, axes = plt.subplots(len(channels), 1, figsize=(11, max(5, 2.6 * len(channels))), squeeze=False)
    fig.suptitle("EMG 踩踏相位：前段與後段比較\n藍＝前段，紅＝後段；虛線＝各段峰值相位", fontsize=14, fontweight="bold")
    for ax, channel in zip(axes[:, 0], channels):
        for phase, color in (("start", "tab:blue"), ("end", "tab:red")):
            sub = profiles[(profiles.channel == channel) & (profiles.phase == phase)]
            phase_zh = "前段" if phase == "start" else "後段"
            peak = summary.loc[summary.channel == channel, f"peak_angle_{phase}"].iloc[0]
            ax.plot(sub.angle_bin_center, sub.mean_percent, color=color, label=f"{phase_zh}（峰值 {peak:.0f}°）")
            ax.fill_between(sub.angle_bin_center.to_numpy(float), (sub.mean_percent - sub.sd_percent).to_numpy(float),
                            (sub.mean_percent + sub.sd_percent).to_numpy(float), color=color, alpha=.15)
        change = summary.loc[summary.channel == channel, "rms_change_percent"].iloc[0]
        shift = summary.loc[summary.channel == channel, "peak_angle_shift"].iloc[0]
        ax.set_title(f"{plot_name(channel)}｜RMS {change:+.1f}%｜峰值位移 {shift:+.0f}°")
        ax.set_ylabel("相對訊號強度 (%)"); ax.set_xlim(0, 360); ax.grid(alpha=.2); ax.legend(loc="upper right")
    axes[-1, 0].set_xlabel(f"{label} (deg)")
    fig.tight_layout(rect=(0, 0, 1, .985)); fig.savefig(directory / "emg_vs_angle_start_vs_end.png", dpi=110); plt.close(fig)
    fig, ax = plt.subplots(figsize=(12, max(4, .55 * len(channels))))
    matrix = np.array([profiles[(profiles.channel == name) & (profiles.phase == "all")].mean_percent.to_numpy(float) for name in channels])
    image = ax.imshow(matrix, aspect="auto", extent=(0, 360, len(channels) - .5, -.5), vmin=0, vmax=100, cmap="magma")
    ax.set_title("全肌肉角度熱圖｜每列各自正規化，顏色只比較同一通道", fontweight="bold")
    ax.set_yticks(range(len(channels)), [plot_name(name) for name in channels]); ax.set_xlabel(f"{label} (deg)")
    ax.set_xticks(range(0, 361, 45)); fig.colorbar(image, ax=ax, label="相對自身參考值 (%)")
    fig.tight_layout(); fig.savefig(directory / "emg_angle_heatmap.png", dpi=110); plt.close(fig)
    fig, axes = plt.subplots(1, len(channels), figsize=(max(5, 2.6 * len(channels)), 3.5), subplot_kw={"projection": "polar"}, squeeze=False)
    for ax, channel in zip(axes[0], channels):
        for phase, color in (("start", "tab:blue"), ("end", "tab:red")):
            sub = profiles[(profiles.channel == channel) & (profiles.phase == phase)]
            theta = np.radians(sub.angle_bin_center.to_numpy(float))
            value = sub.mean_percent.to_numpy(float)
            ax.plot(np.r_[theta, theta[0]], np.r_[value, value[0]], color=color, label=phase)
        ax.set_title(plot_name(channel)); ax.set_theta_zero_location("N"); ax.set_theta_direction(-1)
    axes[0, 0].legend(); fig.tight_layout(); fig.savefig(directory / "emg_polar_start_vs_end.png", dpi=110); plt.close(fig)
    fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    fig.suptitle("逐圈趨勢｜陰影區為前段與後段比較時窗", fontweight="bold")
    for channel in channels:
        sub = cycles[cycles.channel == channel]
        axes[0].plot(sub.time_s, sub.rms_uv.rolling(9, min_periods=3, center=True).mean(), label=plot_name(channel))
        axes[1].plot(sub.time_s, sub.mdf_hz.rolling(9, min_periods=3, center=True).mean(), label=plot_name(channel))
    axes[0].set_ylabel("cycle RMS (uV)"); axes[1].set_ylabel("median frequency (Hz)")
    axes[1].set_xlabel("騎乘時間 (秒)"); axes[0].legend(ncol=2, fontsize=7)
    for ax in axes:
        ax.axvspan(*result["settings"]["start"], color="tab:blue", alpha=.08, label="前段比較時窗")
        ax.axvspan(*result["settings"]["end"], color="tab:red", alpha=.08, label="後段比較時窗")
        ax.grid(alpha=.2)
    fig.tight_layout(rect=(0, 0, 1, .97)); fig.savefig(directory / "emg_fatigue_trends.png", dpi=110); plt.close(fig)
    # Preserve the teacher's reusable full-rate synchronized recording and
    # smaller envelope recording. Re-filter on export to avoid retaining a
    # second copy of a large EMG session in the GUI result.
    t, values, names, fs = _emg(Path(result["sync"]["emg_file"]))
    angle, _ = _angles(Path(result["sync"]["pedal_file"]))
    filtered, envelope = _filter(values, fs)
    session_t = t / result["sync"]["scale"] + result["sync"]["offset_s"]
    angle_t = angle.t_session_s.to_numpy(float)
    fields = {"t_session_s": session_t, "t_emg_file_s": t}
    for key in ("right_pedal_deg", "left_pedal_deg"):
        unwrapped = np.unwrap(np.radians(angle[key].to_numpy(float)))
        fields[key] = np.degrees(np.interp(session_t, angle_t, unwrapped, left=np.nan, right=np.nan)) % 360
    fields["crank_revolutions"] = np.interp(session_t, angle_t, angle.crank_revolutions, left=np.nan, right=np.nan)
    fields["pedaling"] = (np.interp(session_t, angle_t, angle.pedaling, left=0, right=0) > .5).astype(int)
    after = np.searchsorted(angle_t, session_t, side="right")
    before = np.clip(after - 1, 0, len(angle_t) - 1)
    after = np.clip(after, 0, len(angle_t) - 1)
    missing = angle_t[after] - angle_t[before] > .05
    for key in ("right_pedal_deg", "left_pedal_deg", "crank_revolutions"):
        fields[key][missing] = np.nan
    fields["pedaling"][missing] = 0
    base = pd.DataFrame(fields)
    exported_names = [display[name] for name in names]
    if include_raw:
        pd.concat((base, pd.DataFrame(filtered, columns=exported_names)), axis=1).to_csv(
            directory / "emg_synced.csv", index=False, float_format="%.7g", encoding="utf-8-sig")
    if envelope_rate > 0:
        step = max(1, int(round(fs * result["sync"]["scale"] / envelope_rate)))
        small = base.iloc[::step].reset_index(drop=True)
        pd.concat((small, pd.DataFrame(envelope[::step], columns=exported_names)), axis=1).to_csv(
            directory / f"emg_synced_envelope_{int(envelope_rate)}Hz.csv", index=False,
            float_format="%.7g", encoding="utf-8-sig")
    fig, axes = plt.subplots(2, 1, figsize=(10, 6))
    references = result["sync"]["references"]
    if references:
        axes[0].bar([exported_names[item["channel_index"]] for item in references],
                    [item["residual_std_s"] for item in references])
        axes[0].tick_params(axis="x", labelrotation=15)
    axes[0].set_ylabel("clock fit residual SD (s)")
    index = references[0]["channel_index"] if references else 0
    valid = np.isfinite(fields["right_pedal_deg"])
    phase = np.asarray(fields["right_pedal_deg"])[valid]
    bins = np.floor(phase / 15).astype(int) % 24
    profile = [np.mean(envelope[valid, index][bins == k]) if np.any(bins == k) else np.nan for k in range(24)]
    axes[1].plot(np.arange(24) * 15 + 7.5, profile)
    axes[1].set_xlabel(f'{result["sync"]["angle_label"]} (deg)')
    axes[1].set_ylabel(f"{exported_names[index]} envelope (V)")
    fig.tight_layout(); fig.savefig(directory / "sync_check.png", dpi=110); plt.close(fig)
