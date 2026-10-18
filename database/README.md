# SQL Server 資料庫整合

Issue #10 使用既有的 `RehabPatients-ylh` 資料庫與五張表：`Sessions`、`TestBaselines`、`EMGData`、`PedalData`、`TrainerData`。

## 一次性 SSMS 設定

在 SSMS 選取 `RehabPatients-ylh` 後執行 [RehabPatients_001_issue10_constraints.sql](RehabPatients_001_issue10_constraints.sql)。它不會刪除任何資料，會加入：

- 五張明細表到 `Sessions.TestID` 的外鍵。
- 每秒資料的唯一索引，避免同一測試、秒數、肌肉重複寫入。
- `StartedAt`、`EndedAt`、`AnalysisVersion` 與基準來源欄位，供後續追溯。

若資料表已有重複的每秒資料，腳本會停止並提示，不會自動刪除歷史紀錄。

## 桌面程式操作

1. 在「設定 → SQL Server 資料庫」填入 Server、帳號與密碼，再按「測試連線」。
2. 程式會列出此帳號可存取的資料庫，並驗證選取資料庫的五張表。
3. 連線成功後，開始收集會建立同一個 `TestID`，背景寫入每秒 EMG RMS、踏板與訓練台資料。
4. 收集結束後會更新 `Sessions` 的最終功率、踏頻、距離、左右平衡與狀態。

連線資料只存在本次程式執行的記憶體中，不會寫入 Qt 設定、CSV 或專案檔案。後端固定使用 `mssql-python` 的 SQL 帳密連線，並以 `Encrypt=yes;TrustServerCertificate=yes` 支援目前本機 SQL Server 的受信任憑證設定。

CSV、FIT 與 `.analysis.json` 仍會照原本流程輸出。資料庫暫時只存每秒彙整值，原始高頻 EMG 不會寫入 SQL Server。

## 儲存規則

- EMG 每秒每條肌肉儲存實際 200 ms 滑動 RMS 的平均與最大值；不以插值補出缺少的秒數。
- 基準值有效時，保存基準 RMS、偏離百分比與 `NORMAL`／`YELLOW`／`RED` 提示；沒有有效基準則為 `NULL`／`NONE`。
- 踏板左右功率有直接量測時標為「直接量測」；由總功率和比例估算時會標記來源。
- 缺少裝置資料一律寫入 `NULL`，不以 0 代替。
- 資料庫寫入失敗不會停止即時收集或 CSV 輸出，事件會記在程式日誌。
