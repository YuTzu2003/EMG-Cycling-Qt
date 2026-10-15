#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
emg_angle_analysis.py — 用「同步後的 EMG 檔」分析各踏板角度的肌群活化，並比較剛開始 vs 快結束。

前置：先用 emg_pedal_sync.py 產生 emg_synced.csv（每個 EMG 樣本都有 t_session_s、右/左踏板角、圈數）。

用法
----
    python emg_angle_analysis.py emg_synced.csv [--report sync_report.json] [--out out_dir]
        [--start 5 65] [--end 240 300]        比較時段（session 秒）
        [--left S01,S03,...]                  手動指定左腳通道（預設讀 sync_report.json 的 leg_assignment）
        [--bins 24]                           一圈切幾個角度區間
        [--norm 99]                           正規化用的百分位（% of session peak）

輸出（out_dir）
--------------
  emg_per_cycle.csv               逐圈：每圈每通道的 24 區間活化、RMS、中位頻率、尖峰角、活化占比
  emg_angle_profiles.csv          每通道 all/start/end 的平均曲線（均值、SD）
  emg_start_vs_end_summary.csv    開始 vs 結束 統計摘要（Mann–Whitney、趨勢斜率）
  emg_vs_angle_start_vs_end.png   7 肌群 × 左右腳 的活化–角度曲線
  emg_polar_start_vs_end.png      極座標版
  emg_angle_heatmap.png           整段 肌群 × 角度 熱圖
  emg_fatigue_trends.png          逐圈 RMS 與中位頻率趨勢
"""
import argparse, json, os, re, warnings
import numpy as np, pandas as pd
from scipy.signal import welch, butter, sosfiltfilt
from scipy.stats import mannwhitneyu, linregress
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
warnings.filterwarnings('ignore')
plt.rcParams['font.family'] = ['Microsoft JhengHei', 'Noto Sans CJK TC', 'Noto Sans CJK JP', 'DejaVu Sans']; plt.rcParams['axes.unicode_minus'] = False

MUS_ORDER = ['RF', 'VM', 'VL', 'BF', 'GAS', 'SOL', 'TA']
MUS_ZH = {'RF': '股直肌', 'VM': '股內側肌', 'VL': '股外側肌', 'BF': '股二頭肌', 'GAS': '腓腸肌', 'SOL': '比目魚肌', 'TA': '脛前肌'}
MUS_KEY = [('rectus', 'RF'), ('vastus medialis', 'VM'), ('vastus lateralis', 'VL'), ('biceps', 'BF'), ('gastroc', 'GAS'), ('soleus', 'SOL'), ('tibialis', 'TA'),
           ('_rf', 'RF'), ('_vm', 'VM'), ('_vl', 'VL'), ('_bf', 'BF'), ('_gas', 'GAS'), ('_sol', 'SOL'), ('_ta', 'TA')]
def muscle_of(name):
    n = name.lower()
    for k, m in MUS_KEY:
        if k in n: return m
    return None
def circ_mean(a): return np.degrees(np.angle(np.exp(1j * np.radians(a)).mean())) % 360


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('synced'); ap.add_argument('--report', default=None); ap.add_argument('--out', default='emg_out')
    ap.add_argument('--start', nargs=2, type=float, default=[5, 65]); ap.add_argument('--end', nargs=2, type=float, default=[240, 300])
    ap.add_argument('--left', default=None); ap.add_argument('--bins', type=int, default=24); ap.add_argument('--norm', type=float, default=99)
    a = ap.parse_args(); os.makedirs(a.out, exist_ok=True)
    START, END, NB = tuple(a.start), tuple(a.end), a.bins

    d = pd.read_csv(a.synced)
    meta = ['t_session_s', 't_emg_file_s', 'right_pedal_deg', 'left_pedal_deg', 'crank_revolutions', 'pedaling']
    names = [c for c in d.columns if c not in meta]
    ts = d.t_session_s.values; fs = 1.0 / np.median(np.diff(ts))
    print(f'{len(names)} 通道, 有效取樣率 {fs:.1f} Hz, {ts[-1]:.1f} s')
    # 左右腳
    if a.left: left = set(a.left.split(','))
    elif a.report and os.path.exists(a.report):
        left = {k for k, v in json.load(open(a.report, encoding='utf-8'))['leg_assignment'].items() if v == 'L'}
    else:
        raise SystemExit('請用 --left 指定左腳通道，或提供 --report sync_report.json')
    leg = {n: ('L' if n in left else 'R') for n in names}
    muscle = {n: muscle_of(n) for n in names}
    short = {n: (re.match(r'(S\d+)', n).group(1) + '_' + muscle[n]) if re.match(r'(S\d+)', n) and muscle[n] else n for n in names}
    # 包絡線（同步檔是濾波後 EMG）
    sos_lp = butter(4, 6, btype='low', fs=fs, output='sos')
    filt = d[names].values.astype(float); env = np.column_stack([sosfiltfilt(sos_lp, np.abs(filt[:, i])) for i in range(len(names))])
    rev = d.crank_revolutions.values; ped = d.pedaling.values > 0; ok = np.isfinite(rev) & ped
    angR = d.right_pedal_deg.values; angL = d.left_pedal_deg.values
    phase_R = (angR[ok][0] - 360 * rev[ok][0]) % 360          # 事件相位（由角度檔反推）
    cycR = np.floor(rev + phase_R / 360); cycL = np.floor(rev + phase_R / 360 + 0.5)
    centers = np.arange(0, 360, 360 / NB) + 180 / NB
    rows_cyc, norm = [], {}
    for i, n in enumerate(names):
        ang = angL if leg[n] == 'L' else angR; cy = cycL if leg[n] == 'L' else cycR
        norm[n] = np.percentile(env[ok, i], a.norm); en = env[:, i] / norm[n] * 100
        b = np.clip(np.floor(ang / (360 / NB)).astype(int), 0, NB - 1)
        for c in np.unique(cy[ok]):
            m = ok & (cy == c)
            if m.sum() < 0.8 * fs * 60 / 70: continue
            prof = np.array([en[m & (b == k)].mean() if (m & (b == k)).any() else np.nan for k in range(NB)])
            if np.isnan(prof).any(): continue
            z = ((prof - prof.min()) * np.exp(1j * np.radians(centers))).sum(); pk = np.degrees(np.angle(z)) % 360
            active = prof > 0.2 * prof.max() + 0.8 * prof.min()
            seg = filt[m, i]; f, P = welch(seg, fs, nperseg=1024); mdf = f[np.cumsum(P) >= P.sum() / 2][0]
            rows_cyc.append(dict(ch=short[n], leg=leg[n], muscle=muscle[n], cycle=int(c), t_session=ts[m].mean(), mean_pct=prof.mean(), peak_pct=prof.max(),
                                 peak_angle=pk, active_frac=active.mean(), mdf_hz=mdf, rms_uV=np.sqrt((seg ** 2).mean()) * 1e6, **{f'b{k}': prof[k] for k in range(NB)}))
    C = pd.DataFrame(rows_cyc)
    C['phase'] = np.where((C.t_session >= START[0]) & (C.t_session < START[1]), 'start', np.where((C.t_session >= END[0]) & (C.t_session <= END[1]), 'end', 'mid'))
    C.to_csv(f'{a.out}/emg_per_cycle.csv', index=False)

    summ = []
    for n in names:
        ch = short[n]; s = C[(C.ch == ch) & (C.phase == 'start')]; e = C[(C.ch == ch) & (C.phase == 'end')]; allc = C[C.ch == ch]
        if len(s) < 5 or len(e) < 5: continue
        dd = dict(ch=ch, leg=leg[n], muscle=muscle[n], muscle_zh=MUS_ZH.get(muscle[n], ''), n_start=len(s), n_end=len(e), norm_uV=norm[n] * 1e6,
                  mean_pct_start=s.mean_pct.mean(), mean_pct_end=e.mean_pct.mean(), peak_pct_start=s.peak_pct.mean(), peak_pct_end=e.peak_pct.mean(),
                  peak_angle_start=circ_mean(s.peak_angle), peak_angle_end=circ_mean(e.peak_angle), active_frac_start=s.active_frac.mean(), active_frac_end=e.active_frac.mean(),
                  rms_uV_start=s.rms_uV.mean(), rms_uV_end=e.rms_uV.mean(), mdf_start=s.mdf_hz.mean(), mdf_end=e.mdf_hz.mean())
        dd['mean_change_pct'] = 100 * (dd['mean_pct_end'] / dd['mean_pct_start'] - 1); dd['rms_change_pct'] = 100 * (dd['rms_uV_end'] / dd['rms_uV_start'] - 1)
        dd['peak_angle_shift'] = ((dd['peak_angle_end'] - dd['peak_angle_start'] + 180) % 360) - 180; dd['mdf_change_hz'] = dd['mdf_end'] - dd['mdf_start']
        dd['p_mean'] = mannwhitneyu(s.mean_pct, e.mean_pct).pvalue; dd['p_mdf'] = mannwhitneyu(s.mdf_hz, e.mdf_hz).pvalue
        lr = linregress(allc.t_session, allc.mdf_hz); dd['mdf_slope_hz_per_min'] = lr.slope * 60; dd['mdf_slope_p'] = lr.pvalue
        lr2 = linregress(allc.t_session, allc.rms_uV); dd['rms_slope_pct_per_min'] = lr2.slope * 60 / allc.rms_uV.mean() * 100; dd['rms_slope_p'] = lr2.pvalue
        summ.append(dd)
    S = pd.DataFrame(summ); S.to_csv(f'{a.out}/emg_start_vs_end_summary.csv', index=False)

    prof_rows = []
    for n in names:
        ch = short[n]
        for ph in ('all', 'start', 'end'):
            sub = C[C.ch == ch] if ph == 'all' else C[(C.ch == ch) & (C.phase == ph)]
            pr = sub[[f'b{k}' for k in range(NB)]].mean().values; sd = sub[[f'b{k}' for k in range(NB)]].std().values
            for k in range(NB): prof_rows.append(dict(ch=ch, leg=leg[n], muscle=muscle[n], phase=ph, angle_bin_center=centers[k], mean_pct=pr[k], sd_pct=sd[k]))
    P = pd.DataFrame(prof_rows); P.to_csv(f'{a.out}/emg_angle_profiles.csv', index=False)

    # ---------- figures ----------
    chans = {(muscle[n], leg[n]): short[n] for n in names if muscle[n]}
    mus_present = [m for m in MUS_ORDER if (m, 'R') in chans or (m, 'L') in chans]
    fig, axes = plt.subplots(len(mus_present), 2, figsize=(13, 2.9 * len(mus_present)), sharex=True, squeeze=False)
    for r_, mus in enumerate(mus_present):
        for c_, lg in enumerate(('R', 'L')):
            ax = axes[r_, c_]; ch = chans.get((mus, lg))
            if ch is None: ax.axis('off'); continue
            for ph, col in (('start', 'tab:blue'), ('end', 'tab:red')):
                sub = P[(P.ch == ch) & (P.phase == ph)]
                ax.plot(sub.angle_bin_center, sub.mean_pct, color=col, lw=2, label=f'{ph} {START if ph=="start" else END} s')
                ax.fill_between(sub.angle_bin_center, sub.mean_pct - sub.sd_pct, sub.mean_pct + sub.sd_pct, color=col, alpha=.15)
            for xg in (90, 180, 270): ax.axvline(xg, color='gray', lw=.6, ls=':')
            ax.set_title(f'{ch} {MUS_ZH.get(mus, "")} ({"右" if lg == "R" else "左"}腳)', fontsize=10); ax.set_xlim(0, 360); ax.set_ylim(0, 110); ax.grid(alpha=.3)
            if r_ == 0: ax.legend(fontsize=8)
            if c_ == 0: ax.set_ylabel('% of session peak')
    for ax in axes[-1]: ax.set_xlabel('pedal angle (deg)  0=TDC, 90=front, 180=BDC, 270=back'); ax.set_xticks(range(0, 361, 45))
    plt.tight_layout(); plt.savefig(f'{a.out}/emg_vs_angle_start_vs_end.png', dpi=90); plt.close()

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    for c_, lg in enumerate(('R', 'L')):
        ms = [m for m in mus_present if (m, lg) in chans]
        M = np.array([P[(P.ch == chans[(m, lg)]) & (P.phase == 'all')].mean_pct.values for m in ms])
        im = axes[c_].imshow(M, aspect='auto', cmap='magma', extent=[0, 360, len(ms) - .5, -.5], vmin=0, vmax=100)
        axes[c_].set_yticks(range(len(ms))); axes[c_].set_yticklabels([f'{m} {MUS_ZH.get(m, "")}' for m in ms]); axes[c_].set_xticks(range(0, 361, 45))
        axes[c_].set_xlabel('pedal angle (deg)'); axes[c_].set_title(f'{"右" if lg == "R" else "左"}腳 整段平均活化 (% peak)')
    plt.colorbar(im, ax=axes, fraction=.02); plt.savefig(f'{a.out}/emg_angle_heatmap.png', dpi=90, bbox_inches='tight'); plt.close()

    fig, axes = plt.subplots(2, len(mus_present), figsize=(3.4 * len(mus_present), 7.5), subplot_kw=dict(projection='polar'), squeeze=False)
    for c_, mus in enumerate(mus_present):
        for r_, lg in enumerate(('R', 'L')):
            ax = axes[r_, c_]; ch = chans.get((mus, lg))
            if ch is None: ax.axis('off'); continue
            for ph, col in (('start', 'tab:blue'), ('end', 'tab:red')):
                sub = P[(P.ch == ch) & (P.phase == ph)]; th = np.radians(np.r_[sub.angle_bin_center.values, sub.angle_bin_center.values[0]])
                ax.plot(th, np.r_[sub.mean_pct.values, sub.mean_pct.values[0]], color=col, lw=1.8, label=ph)
            ax.set_theta_zero_location('N'); ax.set_theta_direction(-1); ax.set_ylim(0, 100); ax.set_yticks([50, 100]); ax.set_yticklabels(['50', '100%'], fontsize=7)
            ax.set_xticks(np.radians([0, 90, 180, 270])); ax.set_xticklabels(['TDC', '90', 'BDC', '270'], fontsize=8); ax.set_title(f'{ch} ({"右" if lg == "R" else "左"})', fontsize=9, pad=12)
    axes[0, 0].legend(loc='lower left', fontsize=8, bbox_to_anchor=(-0.4, -0.15)); plt.tight_layout(); plt.savefig(f'{a.out}/emg_polar_start_vs_end.png', dpi=90); plt.close()

    fig, axes = plt.subplots(2, len(mus_present), figsize=(3.4 * len(mus_present), 7), squeeze=False)
    for c_, mus in enumerate(mus_present):
        for lg, col in (('R', 'tab:green'), ('L', 'tab:purple')):
            ch = chans.get((mus, lg))
            if ch is None: continue
            sub = C[C.ch == ch]
            axes[0, c_].plot(sub.t_session, sub.rms_uV.rolling(9, center=True, min_periods=3).mean(), color=col, lw=1.2, label=f'{lg} {ch}')
            axes[1, c_].plot(sub.t_session, sub.mdf_hz.rolling(9, center=True, min_periods=3).mean(), color=col, lw=1.2, label=f'{lg} {ch}')
        axes[0, c_].set_title(f'{mus} {MUS_ZH.get(mus, "")}'); axes[0, c_].legend(fontsize=7); axes[1, c_].legend(fontsize=7)
        for ax in axes[:, c_]: ax.grid(alpha=.3); ax.axvspan(*START, color='tab:blue', alpha=.08); ax.axvspan(*END, color='tab:red', alpha=.08)
        axes[1, c_].set_xlabel('session time (s)')
    axes[0, 0].set_ylabel('cycle RMS (uV)'); axes[1, 0].set_ylabel('median frequency (Hz)'); plt.tight_layout(); plt.savefig(f'{a.out}/emg_fatigue_trends.png', dpi=90); plt.close()

    pd.set_option('display.width', 250)
    print(S[['ch', 'leg', 'muscle', 'n_start', 'n_end', 'rms_uV_start', 'rms_uV_end', 'rms_change_pct', 'peak_angle_start', 'peak_angle_end', 'active_frac_start', 'active_frac_end', 'mdf_start', 'mdf_end', 'mdf_change_hz', 'p_mdf']].round(2).to_string())
    print(f'→ {a.out}/')


if __name__ == '__main__':
    main()
