# EMG Rehabilitation Qt Desktop

本資料夾是可獨立安裝與啟動的 EMG 復健 Qt 專案，負責 EMG 裝置、踏板、阻力計及攝影機的裝置設定與原始資料收集。病人建檔、完整分析和歷次比較位於上層 Flask 網站。Qt 將使用者選擇上傳的測試存入與網站共用的 SQL Server 資料庫。

## 安裝與啟動

在 Windows PowerShell 執行：

```powershell
cd collect-Qt
uv sync
uv run main.py
```

直接收集 EMG 原始資料可執行 `uv run emg_acquisition.py`。

資料庫設定共用上層專案的 `../.env`；請先填入 `DB_SERVER`、`DB_NAME`、`DB_USER` 與 `DB_PASSWORD`。Qt 不保存另一份密碼。第一次使用時，在網站新增病人，然後於 Qt 設定頁測試資料庫連線並重新整理病人清單。

收集期間只寫入本機檔案。測試完成後，Qt 會詢問是否上傳至 SQL Server。選擇「上傳到資料庫」會把測試紀錄、EMG／踏板／阻力計量測資料及其原始 CSV 一起存入資料庫，網站的「後續分析」與「原始資料」頁即可查看或下載。選擇「僅保留本機」不會寫入該筆測試；在 Qt 尚未關閉且未開始下一筆測試前，可按「上傳本次測試」補傳或重試。關閉 Qt 後仍可從網站「匯入 CSV」分別選取同次測試的檔案匯入。

SQL 資料表與網站啟動方式見上層 [README](../README.md)。
