## 啟動 GUI

```powershell
uv sync
uv run python main.py
```

## 互動式肌群圖

「收集資料」頁面提供人體正面／背面切換，並依 `sensor_mapping.json` 在左右腿標示感測器位置。點選肌群可顯示或隱藏對應的即時 EMG 波形；感測器清單的勾選狀態也會同步到人體圖。

人體圖使用的 GitHub 參考專案、實際整合方式與授權資訊記錄於 `GITHUB_REFERENCES.txt`。

開始前請在 Trigno SDK Server 中配對並啟用感測器，並確認 Command/EMG/AUX TCP 埠 `50040`、`50041`、`50042` 可連線。

## 介面使用方式

左欄勾選任意多個感測器，即會同時顯示多條獨立波形。為避免畫面雜亂，程式不再顯示每通道即時數值；按「僅啟用」可只顯示目前有啟用輸出的感測器。可調整時間範圍、固定 Y 軸範圍，或開啟自動縮放。

## 命令列錄製

```powershell
uv run python trigno_emg.py --host 127.0.0.1 --duration 10 --output recordings\trial_01.csv
```
