#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
emg_pedal_sync.py — 把 Delsys EMG.csv 與踏板角度檔對齊，輸出「每個 EMG 樣本都有踏板角度」的同步檔。

用法
----
    python emg_pedal_sync.py EMG.csv pedal_angles.csv -o emg_synced.csv
        [--scale auto | 1.0 | 1.0394]      EMG 時間軸比例（auto = 由爆發序列與曲柄事件自動估計）
        [--offset 0 | auto | <秒>]         EMG 0 s 對應的 session 時間（0 = 假設同時起始）
        [--ref-channels S03,S07,...]       用來估時鐘的參考通道（預設自動挑周期性最強的 4 個）
        [--envelope-rate 100]              另外輸出 100 Hz 包絡線檔（小檔，方便畫圖）
        [--no-raw]                         不輸出全取樣率的大檔
        [--report sync_report.json] [--plot sync_check.png]

輸入
----
  EMG.csv          Delsys 匯出：第一欄 time_seconds，其餘為各通道（V）
  pedal_angles.csv pedal_angle.py convert 的輸出：t_session_s, right_pedal_deg, left_pedal_deg,
                   crank_revolutions, pedaling（100 Hz）

同步原理
--------
1. EMG 濾波 20–450 Hz + 60 Hz notch → 整流 → 6 Hz 包絡線。
2. 在參考通道的包絡線上找每一圈的爆發尖峰（burst），得到爆發時刻序列 T_i（EMG 檔時間）。
3. 由角度檔取出曲柄事件時刻 E_j（crank_revolutions 每跨過整數一次；session 時間）。
4. 爆發間隔序列與事件間隔序列做相關，找出 i↔j 的對應（踏頻的起伏讓對應唯一）。
5. 對配對做線性迴歸 T = a·E + b：a 就是 EMG 時間軸相對 session 時間的比例
   （Test2 得到 a = 1.0394 → 檔案標 1925.9 Hz，實際 ≈ 2001.8 Hz）。
   不同肌群的 b 不同（各自的爆發相位不同），所以 b 不能直接當作起點差。
6. 起點差 offset：預設 0（兩檔同時啟動）；--offset auto 會用各肌群尖峰角度與文獻典型值的差
   估一個整體偏移（有 ±25° 左右的不確定度）；若有硬體同步脈衝，用 --offset <秒> 指定最準。
7. session_t = (EMG_t − offset_file) / a，再把角度檔內插到每個 EMG 樣本。

輸出
----
  emg_synced.csv                 全取樣率：t_session_s, t_emg_file_s, right_pedal_deg, left_pedal_deg,
                                 crank_revolutions, pedaling, 各通道濾波後 EMG（V）
  emg_synced_envelope_100Hz.csv  100 Hz 包絡線版（同欄位，EMG 為包絡線）
  sync_report.json               a、隱含取樣率、各參考通道的配對品質、建議的起點偏移
"""
import argparse, json, sys, warnings
warnings.filterwarnings('ignore')
import numpy as np, pandas as pd
from scipy.signal import butter, sosfiltfilt, iirnotch, filtfilt, find_peaks

EXPECTED_PEAK = {'BF': 140, 'VM': 80, 'RF': 30, 'VL': 80, 'TA': 330, 'GAS': 120, 'SOL': 110, 'GM': 80}  # 文獻典型尖峰角（該側踏板）


def load_emg(path):
    d = pd.read_csv(path, encoding='utf-8-sig')
    t = d.iloc[:, 0].values.astype(float); X = d.iloc[:, 1:].values.astype(float)
    fs_file = 1.0 / np.median(np.diff(t))
    return t, X, list(d.columns[1:]), fs_file


def filter_emg(X, fs):
    sos = butter(4, [20, min(450, 0.45 * fs)], btype='band', fs=fs, output='sos')
    b, a = iirnotch(60, 30, fs)
    sos_lp = butter(4, 6, btype='low', fs=fs, output='sos')
    F = np.zeros_like(X); E = np.zeros_like(X)
    for i in range(X.shape[1]):
        y = filtfilt(b, a, sosfiltfilt(sos, X[:, i])); F[:, i] = y; E[:, i] = sosfiltfilt(sos_lp, np.abs(y))
    return F, E


def crank_events(pa):
    """角度檔 → 曲柄事件時刻（crank_revolutions 跨過整數）"""
    t = pa.t_session_s.values; r = pa.crank_revolutions.values; ped = pa.pedaling.values > 0
    ok = np.isfinite(r) & ped
    t, r = t[ok], r[ok]
    ev = []
    for k in range(int(np.ceil(r.min())), int(np.floor(r.max())) + 1):
        i = np.searchsorted(r, k)
        if 0 < i < len(r):
            ev.append(t[i - 1] + (k - r[i - 1]) * (t[i] - t[i - 1]) / (r[i] - r[i - 1]))
    return np.array(ev)


def burst_times(t, env, period_s):
    e = env / env.std()
    pk, _ = find_peaks(e, distance=int(0.6 * period_s / np.median(np.diff(t))), prominence=0.8)
    return t[pk]


def match_and_fit(T, E):
    """爆發序列 T（EMG 時間）對事件序列 E（session 時間）：找索引位移 k 與線性關係 T = a E + b"""
    dT, dE = np.diff(T), np.diff(E)
    best = None
    for k in range(-25, 26):
        if k >= 0: n = min(len(dT), len(dE) - k); a_, b_ = dT[:n], dE[k:k + n]
        else:      n = min(len(dT) + k, len(dE)); a_, b_ = dT[-k:-k + n], dE[:n]
        if n < 30: continue
        r = np.corrcoef(a_, b_)[0, 1]
        if best is None or r > best[0]: best = (r, k, n)
    r, k, n = best
    Tm, Em = (T[:n + 1], E[k:k + n + 1]) if k >= 0 else (T[-k:-k + n + 1], E[:n + 1])
    A = np.polyfit(Em, Tm, 1); res = Tm - np.polyval(A, Em)
    m = np.abs(res) < 3 * np.median(np.abs(res)) + 1e-6
    A = np.polyfit(Em[m], Tm[m], 1); res = Tm[m] - np.polyval(A, Em[m])
    return dict(a=float(A[0]), b=float(A[1]), r_interval=float(r), index_shift=int(k), n_matched=int(m.sum()), resid_std_s=float(res.std()))


def main():
    ap = argparse.ArgumentParser(description='EMG ↔ 踏板角度 同步')
    ap.add_argument('emg'); ap.add_argument('angles'); ap.add_argument('-o', '--output', default='emg_synced.csv')
    ap.add_argument('--scale', default='auto'); ap.add_argument('--offset', default='0')
    ap.add_argument('--ref-channels', default=None); ap.add_argument('--envelope-rate', type=float, default=100.0)
    ap.add_argument('--no-raw', action='store_true'); ap.add_argument('--report', default='sync_report.json'); ap.add_argument('--plot', default='sync_check.png')
    a = ap.parse_args()

    t_file, X, names, fs_file = load_emg(a.emg)
    print(f'EMG: {X.shape[1]} 通道, {len(t_file)} 樣本, 檔案標示 {fs_file:.1f} Hz, 長度 {t_file[-1]:.1f} s')
    F, ENV = filter_emg(X, fs_file)
    pa = pd.read_csv(a.angles)
    E = crank_events(pa); period = np.median(np.diff(E))
    print(f'角度檔: {len(E)} 次曲柄事件, 平均週期 {period:.3f} s')

    # ---- 1. 估 EMG 時間比例 a ----
    report = {'emg_file': a.emg, 'angles_file': a.angles, 'fs_file_hz': fs_file, 'n_crank_events': int(len(E)), 'channels': {}}
    if a.ref_channels:
        refs = [names.index(c) for c in a.ref_channels.split(',')]
    else:   # 自動挑：包絡線在踏頻附近周期性最強（爆發數最接近事件數且間隔相關最高）
        cand = []
        for i in range(X.shape[1]):
            T = burst_times(t_file, ENV[:, i], period)
            if abs(len(T) - len(E)) > 0.15 * len(E): continue
            cand.append((match_and_fit(T, E)['r_interval'], i))
        refs = [i for _, i in sorted(cand, reverse=True)[:4]]
    fits = []
    for i in refs:
        T = burst_times(t_file, ENV[:, i], period); f = match_and_fit(T, E); f['n_bursts'] = int(len(T))
        report['channels'][names[i]] = f; fits.append(f)
        print(f'  參考 {names[i]:>24s}: a={f["a"]:.5f}  r={f["r_interval"]:.2f}  殘差 {f["resid_std_s"]*1000:.0f} ms  (n={f["n_matched"]})')
    a_auto = float(np.median([f['a'] for f in fits]))
    scale = a_auto if a.scale == 'auto' else float(a.scale)
    report.update(scale_auto=a_auto, scale_used=scale, implied_fs_hz=fs_file * scale)
    print(f'時間比例 a = {scale:.5f}（auto 估計 {a_auto:.5f}）→ 實際取樣率 ≈ {fs_file*scale:.1f} Hz')

    # ---- 2. 起點偏移 ----
    t_sess = t_file / scale                                  # 先以 offset=0 換算
    rev = np.interp(t_sess, pa.t_session_s, pa.crank_revolutions, left=np.nan, right=np.nan)
    ped = np.interp(t_sess, pa.t_session_s, pa.pedaling) > 0.5
    ok = np.isfinite(rev) & ped
    angR = np.interp(t_sess, pa.t_session_s, np.unwrap(np.radians(pa.right_pedal_deg)) ) ; angR = np.degrees(angR) % 360
    # 各通道尖峰角（相對右踏板），成對判左右、與文獻模板比較
    peaks = {}
    for i, n in enumerate(names):
        w = np.clip(ENV[ok, i] - np.median(ENV[ok, i]), 0, None); z = (w * np.exp(1j * np.radians(angR[ok]))).sum() / max(w.sum(), 1e-12)
        peaks[n] = float(np.degrees(np.angle(z)) % 360)
    muscle_of = lambda n: next((k for k in EXPECTED_PEAK if k.lower() in n.lower().replace('vastus medialis', 'vm').replace('vastus lateralis', 'vl').replace('rectus femoris', 'rf').replace('biceps femoris', 'bf').replace('tibialis anterior', 'ta').replace('gastrocnemius', 'gas').replace('soleus', 'sol')), None)
    legs, diffs = {}, []
    by_m = {}
    for n in names:
        m = muscle_of(n)
        if m: by_m.setdefault(m, []).append(n)
    for m, chs in by_m.items():
        exp = EXPECTED_PEAK[m]
        if len(chs) == 2:   # 成對：相差 180°，離模板近的當右腳
            d = [abs(((peaks[c] - exp + 180) % 360) - 180) for c in chs]
            r_i = int(np.argmin(d)); legs[chs[r_i]] = 'R'; legs[chs[1 - r_i]] = 'L'
        else:
            for c in chs: legs[c] = 'R' if abs(((peaks[c] - exp + 180) % 360) - 180) < 90 else 'L'
        for c in chs:
            own = peaks[c] if legs[c] == 'R' else (peaks[c] + 180) % 360
            diffs.append(((exp - own + 180) % 360) - 180)
    glob = float(np.degrees(np.angle(np.exp(1j * np.radians(diffs)).mean()))) if diffs else 0.0
    conc = float(abs(np.exp(1j * np.radians(diffs)).mean())) if diffs else 0.0
    cad = 60.0 / period
    offset_auto_s = -glob / 360.0 * period                   # 尖峰比模板早 glob 度 → EMG 比 session 早 glob/360 圈
    report.update(peak_angle_vs_right_pedal_deg=peaks, leg_assignment=legs,
                  template_offset=dict(mean_expected_minus_observed_deg=glob, concentration=conc, offset_s=offset_auto_s,
                                       note='各肌群尖峰相對文獻典型值的整體偏移；正值代表觀測尖峰偏早。僅供參考，±25° 不確定度'))
    if a.offset == 'auto': offset = offset_auto_s
    else: offset = float(a.offset)
    report['offset_used_s'] = offset
    print(f'左右腳指派: ' + ', '.join(f'{k}={v}' for k, v in legs.items()))
    print(f'模板偏移估計: 尖峰平均比文獻早 {glob:.1f}°（集中度 {conc:.2f}）≈ {offset_auto_s:+.3f} s；本次使用 offset = {offset:+.3f} s')

    # ---- 3. 內插角度到每個 EMG 樣本並輸出 ----
    t_sess = t_file / scale + offset
    cols = {'t_session_s': t_sess, 't_emg_file_s': t_file}
    for c in ('right_pedal_deg', 'left_pedal_deg'):
        u = np.degrees(np.unwrap(np.radians(pa[c].values)))
        cols[c] = np.interp(t_sess, pa.t_session_s, u, left=np.nan, right=np.nan) % 360
    cols['crank_revolutions'] = np.interp(t_sess, pa.t_session_s, pa.crank_revolutions, left=np.nan, right=np.nan)
    cols['pedaling'] = (np.interp(t_sess, pa.t_session_s, pa.pedaling, left=0, right=0) > 0.5).astype(int)
    base = pd.DataFrame(cols)
    if not a.no_raw:
        out = base.copy()
        for i, n in enumerate(names): out[n] = F[:, i]
        out.to_csv(a.output, index=False, float_format='%.6g'); print(f'→ {a.output}  ({len(out)} 列, 全取樣率濾波後 EMG + 角度)')
    if a.envelope_rate:
        step = max(1, int(round(fs_file * scale / a.envelope_rate)))
        oe = base.iloc[::step].copy()
        for i, n in enumerate(names): oe[n] = ENV[::step, i]
        name = a.output.replace('.csv', f'_envelope_{int(a.envelope_rate)}Hz.csv')
        oe.to_csv(name, index=False, float_format='%.6g'); print(f'→ {name}  ({len(oe)} 列, 包絡線)')
    json.dump(report, open(a.report, 'w'), indent=1, ensure_ascii=False); print(f'→ {a.report}')

    # ---- 4. 檢查圖 ----
    if a.plot:
        import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
        plt.rcParams['font.family'] = ['Microsoft JhengHei', 'Noto Sans CJK TC', 'Noto Sans CJK JP', 'DejaVu Sans']; plt.rcParams['axes.unicode_minus'] = False
        fig, ax = plt.subplots(2, 1, figsize=(12, 7))
        i = refs[0]; T = burst_times(t_file, ENV[:, i], period); f = report['channels'][names[i]]
        k = f['index_shift']; n = f['n_matched']
        Tm, Em = (T[:len(E) - k], E[k:]) if k >= 0 else (T[-k:], E[:len(T) + k])
        n = min(len(Tm), len(Em)); ax[0].plot(Em[:n], (Tm[:n] - f['b']) / f['a'] - Em[:n], '.', ms=3)
        ax[0].set_title(f'{names[i]} 爆發時刻 vs 曲柄事件：以 a={f["a"]:.5f} 修正後的殘差（應為零均值、無趨勢）'); ax[0].set_xlabel('session time (s)'); ax[0].set_ylabel('residual (s)'); ax[0].grid(alpha=.3)
        b = np.clip(np.floor(angR[ok] / 15).astype(int), 0, 23)
        for i in refs:
            prof = [ENV[ok, i][b == q].mean() for q in range(24)]; ax[1].plot(np.arange(7.5, 360, 15), np.array(prof) / max(prof), label=f'{names[i]} ({legs.get(names[i], "?")})')
        ax[1].set_xlabel('right pedal angle (deg)'); ax[1].set_ylabel('envelope (norm.)'); ax[1].set_title('參考通道的包絡線 vs 右踏板角度（同步後，應呈現清楚的單峰）'); ax[1].legend(); ax[1].grid(alpha=.3)
        plt.tight_layout(); plt.savefig(a.plot, dpi=90); print(f'→ {a.plot}')


if __name__ == '__main__':
    main()
