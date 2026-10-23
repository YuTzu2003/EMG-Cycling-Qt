# EMG Rehabilitation Qt Desktop

本資料夾是可獨立安裝與啟動的 EMG 復健 Qt 專案，負責 EMG 裝置、踏板、阻力計及攝影機的裝置設定與原始資料收集。病人建檔、完整分析和歷次比較位於上層 Flask 網站。兩端透過上層的 `emg_rehab` 共用分析格式與 SQL Server 封存程式，避免複製兩份資料邏輯。

## 安裝與啟動

在 Windows PowerShell 執行：

```powershell
cd emg_rehab_qt
uv sync
uv run main.py
```

直接收集 EMG 原始資料可執行 `uv run emg_acquisition.py`。

資料庫設定共用上層專案的 `../.env`；請先填入 `DB_SERVER`、`DB_NAME`、`DB_USER` 與 `DB_PASSWORD`。Qt 不保存另一份密碼。第一次使用時，在網站新增病人，然後於 Qt 設定頁測試資料庫連線並重新整理病人清單。收集完成後會保留本機 CSV，同時把完整量測與原始檔存入同一個 SQL Server 資料庫。

SQL 資料表與網站啟動方式見上層 [README](../README.md)。