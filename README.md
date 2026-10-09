## 啟動 GUI

```powershell
uv sync
uv run python delsys_gui.py
```

開始前請在 Trigno SDK Server 中配對並啟用感測器，並確認 Command/EMG/AUX TCP 埠 `50040`、`50041`、`50042` 可連線。

## 介面使用方式

左欄勾選任意多個感測器，即會同時顯示多條獨立波形。為避免畫面雜亂，程式不再顯示每通道即時數值；按「僅啟用」可只顯示目前有啟用輸出的感測器。可調整時間範圍、固定 Y 軸範圍，或開啟自動縮放。

## 命令列錄製

```powershell
uv run python trigno_emg.py --host 127.0.0.1 --duration 10 --output recordings\trial_01.csv
```