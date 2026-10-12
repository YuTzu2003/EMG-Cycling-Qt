# EMG Cycling Monitor

Desktop application for collecting Delsys Trigno EMG data and controlling an
FTMS smart trainer, and a P715 Bluetooth Cycling Power pedal.

## Project layout

- `src/delsys/gui.py`: application window, settings, and live-collection UI.
- `src/delsys/emg/`: Trigno protocol, sensor mappings, and EMG workers.
- `src/delsys/resistance/` and `src/delsys/pedal/`: Bluetooth device services
  and recording/export code.

## Start the GUI

```powershell
uv sync
uv run emg-cycling
```

## EMG 基準 RMS（Issue #2）

在「編輯感測器／肌肉對應」中可直接查看及手動設定每一通道的基準 RMS（mV），也可按「測量 20 秒基準」：前 10 秒捨棄，後 10 秒使用濾波後的 RMS `sqrt(mean(x²))` 建立基準。即時 EMG 會在後台固定以 20–450 Hz band-pass 濾波，再使用 200 ms 滑動 RMS 與已儲存基準比較；一般測試的 CSV 格式保持不變。

已有原始量測檔時，可按「匯入基準 CSV」選取本程式輸出的 EMG CSV。檔案需包含 `time_seconds` 與可對應目前肌肉名稱的 EMG 欄位，且第一段資料至少連續 20 秒；系統同樣捨棄前 10 秒後建立基準，且不會改寫來源檔案。

正式串流會以相同濾波和 200 ms 滑動 RMS 顯示目前值、基準、帶正負號的差異與百分比。可手動修改基準、設定警告/重點察看偏離門檻、啟用或停用研究用顏色提示；設定保存在使用者 AppConfig 的 `emg_baseline_settings.json`。黃色和紅色僅表示相對偏離程度，動作流程、濾波參數與門檻均須先由臨床人員確認，不能作為診斷。

## Record from the command line

```powershell
uv run emg-record --help
```
