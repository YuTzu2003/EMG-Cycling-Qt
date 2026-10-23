# EMG Rehabilitation System

EMG 與踩踏資料的收集、病人管理及分析系統。桌面程式負責裝置設定與原始資料收集；Flask 網站負責病人資料、紀錄查閱、分析及比較。兩者共用 SQL Server 資料庫 `RehabPatients-ylh`。

## 專案結構

```text
emg_rehab/              EMG 復健系統共用核心、資料庫存取與 Flask 網站
  archive.py            病人、測試及原始檔案存取
  storage_db.py         SQL Server 存取與每秒資料寫入
  web.py                Flask 路由
  templates/            網站頁面
  static/               網站樣式
emg_rehab_qt/           EMG 復健桌面收集專案
  emg_rehab_qt/         Qt 介面、裝置連線及資料收集套件
  app.py                Flask 網站啟動檔
  main.py               Qt 桌面程式啟動檔
  emg_acquisition.py    命令列收集程式啟動檔
  pyproject.toml        Qt 專用依賴與啟動指令
  README.md             Qt 安裝與操作說明
sql/001_web_archive.sql 資料庫建表與既有病人資料匯入
tests/                  網站與共用功能測試
```

程式已直接放在專案根目錄，不需要設定 `PYTHONPATH=src`。Qt 的安裝與操作見 [Qt 說明](emg_rehab_qt/README.md)。

## 安裝與資料庫

需要 Windows、Python 3.12 以上、SQL Server，以及 [uv](https://docs.astral.sh/uv/)。先在專案根目錄執行：

```powershell
uv sync
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

如果本機已有 `.env`，保留現有密碼，無須重新複製。以下設定供網站及 Qt 共用：

```dotenv
DB_SERVER=127.0.0.1,1433
DB_NAME=RehabPatients-ylh
DB_USER=Rehab-User
DB_PASSWORD=你的 SQL Server 密碼
WEB_PASSWORD=網站登入密碼
WEB_SECRET_KEY=足夠長的隨機簽章金鑰
WEB_BEHIND_HTTPS_PROXY=0
```

`.env` 已被 Git 忽略。網站需要另外設定 `WEB_PASSWORD` 才能登入；對外開放前也應設定 `WEB_SECRET_KEY`。程式首次連線時會建立網站需要的資料表；也可以在 SSMS 執行 [建表腳本](sql/001_web_archive.sql)。現有 `Sessions` 病人會匯入 `Patients`，原本的收集資料表繼續使用。

## 啟動

網站在根目錄啟動：

```powershell
uv run app.py
```

Qt 在獨立資料夾啟動：

```powershell
cd emg_rehab_qt
uv sync
uv run main.py
```

網站預設位址為 `http://127.0.0.1:5000`。若要從手機連入，請先設定網站密碼及 HTTPS 反向代理，再將代理指向 `127.0.0.1:5000`；代理正確傳遞 `X-Forwarded-Proto` 與 `X-Forwarded-For` 時，設定 `WEB_BEHIND_HTTPS_PROXY=1`。

## 使用流程

1. 在網站建立病人資料。
2. 在 Qt 選擇病人、設定裝置並開始收集；每秒摘要及完整原始檔都寫入 SQL Server。
3. 在網站選取測試、上傳 CSV、查看分析及比較結果。

測試：

```powershell
uv run python -m unittest discover -s tests -v
```
