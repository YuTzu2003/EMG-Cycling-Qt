from __future__ import annotations
import html
from pathlib import Path
from collections.abc import Mapping, Iterable
from emg_rehab.emg.session_analysis import analyze, finite
from emg_rehab.pedal.analysis import analyze_pedal_records

def fmt(value, unit='', digits=1):
    value=finite(value)
    return '--' if value is None else f'{value:.{digits}f}{unit}'
def esc(value): return html.escape('--' if value in (None,'') else str(value))
def tbl(headers, rows):
    body=''.join('<tr>'+''.join(f'<td>{esc(v)}</td>' for v in row)+'</tr>' for row in rows) or f'<tr><td colspan="{len(headers)}">無資料</td></tr>'
    return '<div class="table-wrap"><table width="100%"><thead><tr>'+''.join(f'<th>{esc(h)}</th>' for h in headers)+'</tr></thead><tbody>'+body+'</tbody></table></div>'
def sec(title, body, note=''):
    # Use an ordinary block so the report remains easy to style and print.
    return f'<div class="report-section"><h2>{esc(title)}</h2>'+(f'<p class="note">{esc(note)}</p>' if note else '')+body+'</div>'
def chart(title, rows, key, color, unit=''):
    pairs=[(finite(r.get('time_s')),finite(r.get(key))) for r in rows]
    pairs=[p for p in pairs if p[0] is not None and p[1] is not None]
    if not pairs:return '<p class="note">無資料</p>'
    xmax=max(x for x,y in pairs) or 1; ymin=min(y for x,y in pairs); ymax=max(y for x,y in pairs); span=ymax-ymin or 1; step=max(1,len(pairs)//800)
    poly=' '.join(f'{42+714*x/xmax:.1f},{170-130*(y-ymin)/span:.1f}' for x,y in pairs[::step])
    return f'<figure><figcaption>{esc(title)}</figcaption><svg viewBox="0 0 780 200"><line x1="42" y1="170" x2="756" y2="170"/><line x1="42" y1="22" x2="42" y2="170"/><polyline fill="none" stroke="{color}" stroke-width="2.5" points="{poly}"/><text x="42" y="192">0 s</text><text x="685" y="192">{xmax:.0f} s</text><text x="47" y="18">{ymax:.3g}{unit}</text><text x="47" y="166">{ymin:.3g}{unit}</text></svg></figure>'

def render_report_html(session: Mapping, *, pedal_records: Iterable[Mapping]=(), ftp_w: int=200, angle_result=None, reference_session=None):
    result=analyze(dict(session)); subject=session.get('subject',{}) or {}; channels=session.get('channels',{}) or {}
    pedal_rows=list(pedal_records) or list(session.get('pedal',[])); pedal_more=analyze_pedal_records(pedal_rows,int(ftp_w)) if pedal_rows else {}
    patient=tbl(['項目','內容','項目','內容'],[('病人 ID',subject.get('patientId'),'姓名',subject.get('name')),('性別',subject.get('gender'),'測試開始',session.get('started_at')),('EMG 取樣率',fmt(session.get('sample_rate_hz'),' Hz'),'測試時間',fmt(result['duration_s'],' s',0)),('RMS 特徵',session.get('feature'),'分析版本',session.get('version'))])
    setup=[]
    for slot,ch in sorted(channels.items(),key=lambda x:str(x[0])):
        base=(session.get('baselines',{}) or {}).get(str(slot),{}) or {}; setup.append((f'S{int(slot):02d}' if str(slot).isdigit() else slot,ch.get('label') or ch.get('mapping_name'),ch.get('mapping_name'),fmt(base.get('rms_mv'),' mV',4)))
    emg=[]; emg_charts=[]
    for slot,data in sorted(result['emg'].items(),key=lambda x:str(x[0])):
        ch=channels.get(str(slot),{}); emg.append((f'S{int(slot):02d}' if str(slot).isdigit() else slot,ch.get('label') or ch.get('mapping_name'),fmt(data['rms_mv']['mean'],' mV',4),fmt(data['rms_mv']['max'],' mV',4),fmt(data['early_rms_mv'],' mV',4),fmt(data['late_rms_mv'],' mV',4),'; '.join(data.get('quality',[])) or '有效'))
        label=ch.get('label') or ch.get('mapping_name') or f'S{slot}'; emg_charts.append(chart(f'S{slot} {label} RMS 趨勢',[r for r in session.get('emg',[]) if str(r.get('slot'))==str(slot)],'rms_mv','#1261c9',' mV'))
    pedal=[('平均功率',fmt(result['pedal']['power_w']['mean'],' W')),('平均踏頻',fmt(result['pedal']['cadence_rpm']['mean'],' rpm')),('左右平衡（左）',fmt(result['pedal']['left_balance_percent']['mean'],' %')),('累積踏踏輸出',fmt(pedal_more.get('energy_kj'),' kJ')),('5 秒 / 60 秒最佳平均',fmt(pedal_more.get('peak_5s_w'),' W')+' / '+fmt(pedal_more.get('peak_60s_w'),' W')),('資料涵蓋',fmt(pedal_more.get('coverage_percent'),' %',0))]
    trainer=[('平均速度',fmt(result['trainer']['speed_kph']['mean'],' km/h')),('平均功率',fmt(result['trainer']['power_watts']['mean'],' W')),('平均踏頻',fmt(result['trainer']['cadence_rpm']['mean'],' rpm')),('累積距離',fmt(result['trainer']['session_distance_meters']['max'],' m',0)),('固定阻力等級',fmt(result['trainer']['resistance_level']['mean'],digits=0)),('設備回報阻力',fmt(result['trainer']['resistance_native_reported']['mean']))]
    segments=[(fmt(s.get('resistance')),fmt(s.get('seconds'),' s',0),fmt(s.get('power_w'),' W'),fmt(s.get('cadence_rpm'),' rpm')) for s in result.get('resistance_segments',[])]
    angle=[]; summary=angle_result.get('summary') if isinstance(angle_result,Mapping) else None
    if hasattr(summary,'to_dict'):
        for r in summary.to_dict('records'): angle.append((r.get('muscle',r.get('muscle_name','--')),fmt(r.get('rms_start')),fmt(r.get('rms_end')),fmt(r.get('rms_change_pct'),' %'),fmt(r.get('peak_angle_start'),'°',0),fmt(r.get('peak_angle_end'),'°',0)))
    compare=''
    if isinstance(reference_session,Mapping):
        old=analyze(dict(reference_session)); rows=[(f'S{x}',fmt(old['emg'].get(x,{}).get('rms_mv',{}).get('mean'),' mV',4),fmt(result['emg'].get(x,{}).get('rms_mv',{}).get('mean'),' mV',4)) for x in sorted(set(old['emg'])|set(result['emg']))]; compare=sec('歷次測試比較',tbl(['肌肉','參考量測 RMS','本次 RMS'],rows))
    warning='<ul>'+''.join(f'<li>{esc(x)}</li>' for x in result['warnings'])+'</ul>' if result['warnings'] else '<p>無額外資料品質提示。</p>'
    body=sec('病人與測試資訊',patient)+sec('測試設定',tbl(['Sensor','肌肉','對應名稱','套用基準 RMS'],setup),'基準 RMS 為本次測試快照。')+sec('EMG 肌電分析',tbl(['Sensor','肌肉','平均 RMS','最大 RMS','前段 RMS','後段 RMS','資料品質'],emg))+sec('各肌肉 RMS 趨勢圖','<div class="charts">'+''.join(emg_charts)+'</div>','每張圖呈現對應肌肉的 RMS 隨測試時間變化。')+sec('踏板分析',tbl(['指標','數值'],pedal)+chart('踏板功率趨勢',session.get('pedal',[]),'power_w','#d92d20',' W'))+sec('阻力計分析',tbl(['指標','數值'],trainer)+chart('阻力計功率趨勢',session.get('trainer',[]),'power_watts','#7e57c2',' W'))+sec('阻力區間分析',tbl(['阻力','時間','平均功率','平均踏頻'],segments))+sec('踏踏角度分析',tbl(['肌肉','前段 RMS','後段 RMS','RMS 變化','前段峰值','後段峰值'],angle),'未執行角度分析時顯示無資料。')+compare+sec('資料品質與說明',warning)
    return f'''<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>EMG Cycling 測試報告</title><style>:root{{font-family:"Segoe UI","Microsoft JhengHei",sans-serif;color:#172033}}body{{max-width:1120px;margin:auto;padding:34px;background:#f3f6f9;line-height:1.55}}header{{background:#113f67;color:white;border-radius:12px;padding:26px 30px}}header p{{color:#dcecf9;margin:4px 0}}h1,h2{{margin:0}}h2{{font-size:18px;color:#123b60;margin-bottom:12px}}.report-section{{background:white;border:1px solid #dbe5ee;border-radius:10px;padding:20px;margin:14px 0;break-inside:avoid}}table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{padding:9px;border-bottom:1px solid #e6edf3;text-align:left}}th{{background:#edf5fc;color:#264d70}}.table-wrap{{overflow:auto}}.note{{color:#63768a;font-size:13px}}.charts{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}}figure{{margin:0;padding:10px;border:1px solid #e0e8ef;border-radius:8px}}figcaption{{font-weight:bold;color:#254c70}}svg{{width:100%}}svg line{{stroke:#a6b7c8}}svg text{{font-size:11px;fill:#64748b}}@media print{{body{{padding:0;background:white}}header{{border-radius:0}}}}@media(max-width:700px){{body{{padding:12px}}}}</style><body><header><p>EMG CYCLING MONITOR</p><h1>肌電與踏踏測試報告</h1><p>供復健團隊與主治醫師檢視</p></header>{body}<footer>本報告用於資料整理與趨勢呈現，不代表臨床診斷。</footer></body></html>'''
def export_report(path: Path, session: Mapping, **kwargs):
    """Export the current analysis as a standalone HTML report."""
    document = render_report_html(session, **kwargs)
    path = path.with_suffix('.html')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding='utf-8')
    return path



