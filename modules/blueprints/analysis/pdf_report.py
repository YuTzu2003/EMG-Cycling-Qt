from __future__ import annotations
import io
import math
import textwrap
from collections import defaultdict
from pathlib import Path
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure
from matplotlib.font_manager import FontProperties
from matplotlib.lines import Line2D

def _font() -> FontProperties:
    candidates = (
        Path('C:/Windows/Fonts/NotoSansTC-VF.ttf'),
        Path('C:/Windows/Fonts/msjh.ttc'),
        Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
        Path('/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc'),
    )
    path = next((item for item in candidates if item.is_file()), None)
    if path is None:
        raise RuntimeError('找不到可用的中文字型，無法建立 PDF。')
    return FontProperties(fname=str(path))


def _number(value, unit='', digits=1) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return '—'
    return f'{number:.{digits}f}{unit}' if math.isfinite(number) else '—'


def _text(value) -> str:
    return str(value if value is not None else '—')


def _page(title: str, font: FontProperties, *, landscape: bool = True) -> Figure:
    figure = Figure(figsize=(11.69, 8.27) if landscape else (8.27, 11.69), facecolor='white')
    figure.text(.055, .945, '復健騎乘分析', fontproperties=font, fontsize=9, color='#626268')
    figure.text(.055, .902, title, fontproperties=font, fontsize=17, color='#1d1d1f')
    figure.add_artist(Line2D([.055, .945], [.882, .882], transform=figure.transFigure, color='#d4d4d8', linewidth=1))
    figure.text(.945, .035, '復健騎乘分析 · 測試資料報告', ha='right', fontproperties=font, fontsize=8, color='#626268')
    return figure


def _lines(pdf: PdfPages, title: str, lines: list[str], font: FontProperties) -> None:
    wrapped = []
    for line in lines:
        wrapped.extend(textwrap.wrap(_text(line), width=55, break_long_words=True) or [''])
    for start in range(0, max(len(wrapped), 1), 31):
        figure = _page(title if start == 0 else f'{title}（續）', font)
        y = .845
        for line in wrapped[start:start + 31]:
            figure.text(.065, y, line, fontproperties=font, fontsize=10, color='#34343a')
            y -= .025
        pdf.savefig(figure)


def _table(pdf: PdfPages, title: str, headers: list[str], rows: list[tuple], font: FontProperties,
           *, widths: list[float] | None = None, per_page: int = 17) -> None:
    if not rows:
        _lines(pdf, title, ['沒有可列出的資料。'], font)
        return
    for start in range(0, len(rows), per_page):
        figure = _page(title if start == 0 else f'{title}（續）', font, landscape=True)
        axis = figure.add_axes([.045, .11, .91, .72])
        axis.axis('off')
        segment = rows[start:start + per_page]
        cells = [[_text(value)[:45] for value in row] for row in segment]
        table = axis.table(cellText=cells, colLabels=headers, colWidths=widths,
                           cellLoc='left', colLoc='left', bbox=[0, 0, 1, 1])
        table.auto_set_font_size(False)
        table.set_fontsize(8)
        for (row_index, _), cell in table.get_celld().items():
            cell.set_edgecolor('#e6e6e9')
            cell.set_linewidth(.5)
            cell.set_facecolor('#f7f7f8' if row_index == 0 else 'white')
            cell.get_text().set_fontproperties(font)
            cell.get_text().set_fontsize(8)
        pdf.savefig(figure)


def _plot_pages(pdf: PdfPages, title: str, series: list[tuple[str, list[tuple[float, float]], str]],
                font: FontProperties, *, unit: str = '') -> None:
    if not series:
        return
    for start in range(0, len(series), 4):
        figure = _page(title if start == 0 else f'{title}（續）', font, landscape=True)
        for index, (label, points, color) in enumerate(series[start:start + 4], 1):
            axis = figure.add_subplot(2, 2, index)
            stride = max(1, len(points) // 1000)
            sampled = points[::stride]
            if sampled:
                axis.plot([x for x, _ in sampled], [y for _, y in sampled], color=color, linewidth=1.5)
            axis.set_title(label, fontproperties=font, fontsize=10, loc='left')
            axis.set_xlabel('秒' if unit != '%' else '角度', fontproperties=font, fontsize=8)
            axis.set_ylabel(unit, fontproperties=font, fontsize=8)
            axis.tick_params(labelsize=7)
            axis.grid(color='#e6e6e9', linewidth=.6)
        figure.subplots_adjust(left=.08, right=.95, bottom=.09, top=.82, hspace=.47, wspace=.23)
        pdf.savefig(figure)


def _points(rows: list[dict], key: str) -> list[tuple[float, float]]:
    points = []
    for row in rows:
        try:
            x, y = float(row.get('time_s')), float(row.get(key))
        except (TypeError, ValueError):
            continue
        if math.isfinite(x) and math.isfinite(y):
            points.append((x, y))
    return points


def _angle_profiles(pdf: PdfPages, profiles, font: FontProperties) -> None:
    if not hasattr(profiles, 'groupby') or profiles.empty:
        return
    channels = list(profiles.groupby('channel', sort=True))
    for start in range(0, len(channels), 4):
        figure = _page('踩踏角度肌肉曲線' if start == 0 else '踩踏角度肌肉曲線（續）', font,
                       landscape=True)
        for index, (channel, data) in enumerate(channels[start:start + 4], 1):
            axis = figure.add_subplot(2, 2, index)
            for phase, label, color in [('start', '前段', '#34343a'), ('end', '後段', '#176b62')]:
                subset = data[data['phase'] == phase].sort_values('angle_bin_center')
                if not subset.empty:
                    axis.plot(subset['angle_bin_center'], subset['mean_percent'],
                              label=label, color=color, linewidth=1.6)
            axis.set_title(_text(data['channel_label'].iloc[0] if 'channel_label' in data else channel),
                           fontproperties=font, fontsize=10, loc='left')
            axis.set_xlabel('角度', fontproperties=font, fontsize=8)
            axis.set_ylabel('活化比例 %', fontproperties=font, fontsize=8)
            axis.tick_params(labelsize=7)
            axis.grid(color='#e6e6e9', linewidth=.6)
            if axis.lines:
                axis.legend(prop=font, fontsize=7, loc='best')
        figure.subplots_adjust(left=.08, right=.95, bottom=.09, top=.82, hspace=.47, wspace=.23)
        pdf.savefig(figure)


def build_session_pdf(record: dict, document: dict, analysis: dict, *, pedal_summary=None,
                      pedal_performance=None, files: list[dict] | None = None,
                      angle_result: dict | None = None, angle_error: str | None = None) -> bytes:
    """Create one PDF containing the available EMG, pedal, trainer and angle results."""
    font = _font()
    output = io.BytesIO()
    channels = document.get('channels') or {}
    emg = document.get('emg') or []
    pedal = document.get('pedal') or []
    trainer = document.get('trainer') or []
    with PdfPages(output, metadata={'Title': '復健騎乘分析測試報告', 'Author': '復健騎乘分析'}) as pdf:
        _lines(pdf, '測試概況', [
            f"受測者：{record.get('patient_name') or record.get('name') or record.get('patient_id')}",
            f"病歷編號：{record.get('patient_id')}",
            f"測試開始：{_text(record.get('started_at'))[:19].replace('T', ' ')}",
            f"測試狀態：{_text(record.get('status'))}",
            f"資料來源：{'Qt 收集' if record.get('source') == 'qt' else 'CSV 匯入'}",
            f"騎乘時間：{_number(analysis.get('duration_s'), ' 秒', 0)}",
            f"肌電資料：{len(emg)} 筆；踏板資料：{len(pedal)} 筆；訓練器資料：{len(trainer)} 筆",
            '',
            '本報告以資料庫中的同次測試產生。沒有保存原始訊號或曲柄事件時，無法重建完整的踩踏角度分析。',
        ], font)

        emg_rows = []
        by_slot = defaultdict(list)
        for point in emg:
            by_slot[str(point.get('slot'))].append(point)
        for slot, item in sorted((analysis.get('emg') or {}).items(), key=lambda pair: str(pair[0])):
            key = str(slot)
            metadata = channels.get(key) or {}
            peak = max((float(point['rms_peak_mv']) for point in by_slot[key]
                        if point.get('rms_peak_mv') is not None), default=None)
            emg_rows.append((f'S{int(slot):02d}' if key.isdigit() else f'S{key}',
                             metadata.get('label') or metadata.get('mapping_name') or '未設定',
                             _number((item.get('rms_mv') or {}).get('mean'), digits=3),
                             _number(peak, digits=3),
                             _number(item.get('early_rms_mv'), digits=3),
                             _number(item.get('late_rms_mv'), digits=3),
                             '、'.join(item.get('quality') or []) or '良好'))
        _table(pdf, '肌電分析', ['通道', '肌肉', '平均 RMS', '峰值 RMS', '前段 RMS', '後段 RMS', '品質'],
               emg_rows, font, widths=[.08, .26, .12, .12, .12, .12, .18])
        emg_series = [(f"S{slot} {(_text((channels.get(slot) or {}).get('label') or '肌肉'))}",
                       _points(points, 'rms_peak_mv') or _points(points, 'rms_mv'), '#176b62')
                      for slot, points in sorted(by_slot.items())]
        _plot_pages(pdf, '肌電 RMS 包絡線', emg_series, font, unit='mV')

        if pedal:
            details = []
            if pedal_summary:
                details.extend([
                    ('測試時間', _number(pedal_summary.get('duration_seconds'), ' 秒', 0)),
                    ('平均 / 最大功率', f"{_number(pedal_summary.get('average_power_w'), ' W')} / {_number(pedal_summary.get('maximum_power_w'), ' W')}"),
                    ('平均 / 最大踏頻', f"{_number(pedal_summary.get('average_cadence_rpm'), ' rpm')} / {_number(pedal_summary.get('maximum_cadence_rpm'), ' rpm')}"),
                    ('左 / 右平衡', f"{_number(pedal_summary.get('average_left_balance_percent'), ' %')} / {_number(pedal_summary.get('average_right_balance_percent'), ' %')}"),
                    ('左 / 右估計功率', f"{_number(pedal_summary.get('average_left_power_w'), ' W')} / {_number(pedal_summary.get('average_right_power_w'), ' W')}"),
                ])
            if pedal_performance:
                for label, key, unit in [('正規化功率', 'normalized_power_w', ' W'), ('強度係數', 'intensity_factor', ''),
                                         ('訓練負荷 TSS', 'tss', ''), ('累積輸出', 'energy_kj', ' kJ'),
                                         ('5 秒峰值', 'peak_5s_w', ' W'), ('60 秒峰值', 'peak_60s_w', ' W')]:
                    details.append((label, _number(pedal_performance.get(key), unit)))
            _table(pdf, '踏板摘要與訓練效能', ['指標', '數值'], details, font, widths=[.36, .64])
            pedal_series = [(title, _points(pedal, key), color) for title, key, color in
                            [('功率', 'power_w', '#8a551e'), ('踏頻', 'cadence_rpm', '#176b62'),
                             ('左腳平衡', 'left_balance_percent', '#34343a')]]
            _plot_pages(pdf, '踏板時間序列', pedal_series, font)
            zones = (pedal_performance or {}).get('zones') or []
            if zones:
                _table(pdf, '踏板訓練區間', ['區間', '功率範圍', '時間', '比例'],
                       [(zone.get('name'), zone.get('bounds'), f"{zone.get('seconds')} 秒",
                         _number(zone.get('percent'), ' %')) for zone in zones], font)
        else:
            _lines(pdf, '踏板分析', ['此測試沒有踏板時間序列資料。'], font)

        if trainer:
            trainer_rows = []
            for label, key, unit in [('平均速度', 'speed_kph', ' km/h'), ('平均功率', 'power_watts', ' W'),
                                     ('平均踏頻', 'cadence_rpm', ' rpm'), ('最高累積距離', 'session_distance_meters', ' m'),
                                     ('平均阻力等級', 'resistance_level', '')]:
                stats = (analysis.get('trainer') or {}).get(key) or {}
                trainer_rows.append((label, _number(stats.get('max') if key == 'session_distance_meters' else stats.get('mean'), unit)))
            _table(pdf, '訓練器摘要', ['指標', '數值'], trainer_rows, font, widths=[.36, .64])
            _plot_pages(pdf, '訓練器功率', [('功率', _points(trainer, 'power_watts'), '#34343a')], font, unit='W')

        segments = analysis.get('resistance_segments') or []
        if segments:
            _table(pdf, '阻力區段', ['阻力', '時間', '平均功率', '平均踏頻'],
                   [(_number(row.get('resistance')), _number(row.get('seconds'), ' 秒', 0),
                     _number(row.get('power_w'), ' W'), _number(row.get('cadence_rpm'), ' rpm'))
                    for row in segments], font)

        if angle_result:
            _lines(pdf, '踩踏角度分析重點', [angle_result.get('overview') or '—'], font)
            summary = angle_result.get('summary')
            if hasattr(summary, 'to_dict'):
                angle_rows = [(row.get('channel_label') or row.get('channel'),
                               _number(row.get('rms_uv_start'), ' μV'), _number(row.get('rms_uv_end'), ' μV'),
                               _number(row.get('rms_change_percent'), ' %'),
                               _number(row.get('peak_angle_start'), '°', 0),
                               _number(row.get('peak_angle_end'), '°', 0))
                              for row in summary.to_dict('records')]
                _table(pdf, '踩踏角度前後段', ['肌肉', '前段 RMS', '後段 RMS', '變化', '前段峰值', '後段峰值'],
                       angle_rows, font, widths=[.3, .14, .14, .14, .14, .14])
            _angle_profiles(pdf, angle_result.get('profiles'), font)
            sync = angle_result.get('sync') or {}
            _lines(pdf, '踩踏角度設定與同步', [
                f"有效踩踏圈：{_text(sync.get('valid_cycles'))}",
                f"角度標示：{_text(sync.get('angle_label'))}",
                f"時間比例：{_number(sync.get('scale_used'), digits=5)}",
                f"起點偏移：{_number(sync.get('offset_used_s'), ' 秒', 3)}",
            ], font)
        else:
            _lines(pdf, '踩踏角度分析', [angle_error or '此測試尚無足夠的原始 EMG 與踏板角度資料。'], font)

        if analysis.get('warnings'):
            _lines(pdf, '資料品質提示', list(analysis['warnings']), font)

        raw_files = files or []
        _table(pdf, '原始資料概況', ['來源', '檔名', '大小'],
               [(item.get('kind'), item.get('filename'), f"{item.get('size', 0) / 1024:.1f} KB")
                for item in raw_files] or [('資料庫紀錄', '未另外保存 CSV；請於網站查看資料庫分析', '—')],
               font, widths=[.18, .64, .18])
    return output.getvalue()
