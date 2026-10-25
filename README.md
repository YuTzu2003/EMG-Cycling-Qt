## EMG 復健測試系統

本專案包含兩個程式：Flask 網站用於病人管理、測試資料查詢與分析；`collect-Qt` 桌面程式用於裝置設定、即時查看與收集 EMG、踏板及訓練器資料，兩端使用同一個 SQL Server 資料庫。


### 執行需求

- Windows 與 Python 3.12 以上。
- [uv](https://docs.astral.sh/uv/)。
- 可連線的 SQL Server，並已建立專案所需的資料表。現有資料庫結構可參考 `sql/RehabPatients.sql`；網站封存表腳本位於 `sql/001_web_archive.sql`。執行腳本前請先確認目標資料庫與現有資料表，避免重複建立。

### 設定資料庫

在專案根目錄執行：
```powershell
Copy-Item .env.example .env
```

編輯 `.env`，填入自己的連線資訊與密碼。
```dotenv
DB_SERVER=127.0.0.1,1433
DB_NAME=RehabPatients-ylh
DB_USER=Rehab-User
DB_PASSWORD=請填入自己的資料庫密碼
WEB_SECRET_KEY=請填入一組隨機長字串
WEB_BEHIND_HTTPS_PROXY=0
```

### 啟動網站
在專案根目錄執行：
```powershell
uv sync
uv run app.py
```

本機開啟 `http://127.0.0.1:5000`。網站可建立病人、選擇資料庫測試、查看肌電與踏板分析、比較測試，以及匯入 CSV。踩踏角度分析需要同一次測試的原始 EMG 與踏板角度或曲柄事件資料；缺少資料時，頁面會顯示各分析項目的預留位置與提示。

### 啟動 Qt 收集程式
在另一個 PowerShell 視窗執行：
```powershell
cd collect-Qt
uv sync
uv run main.py
```

Qt 使用根目錄的 `.env` 連線資料庫。先在網站建立病人，再於 Qt 選擇病人並收集資料。測試完成後可選擇是否將整筆紀錄及 EMG、踏板、阻力計原始 CSV 存入 SQL Server；選擇不上傳時，本機檔案仍會保留。Qt 的詳細操作請參閱 [collect-Qt/README.md](collect-Qt/README.md)。
