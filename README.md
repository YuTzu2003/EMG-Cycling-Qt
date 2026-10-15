# EMG Cycling Monitor

Windows 桌面程式，將 Delsys Trigno EMG、P715 踏板功率計與 FTMS 智慧訓練台資料錄製在同一次騎乘中。程式提供 20 秒 EMG 基準測量、即時 RMS、騎乘摘要、兩次獨立騎乘的肌肉活化比較、歷史紀錄載入及離線 HTML 報表。分析為研究用途；RMS 是肌肉電活動，不能直接當作肌力或診斷。

## 環境部署

1. 準備 Windows 10/11、Python 3.12 以上、支援 Bluetooth LE 的電腦，並安裝 [uv](https://docs.astral.sh/uv/getting-started/installation/)。
2. 若要收集 EMG，安裝並啟動 Delsys Trigno SDK Server，確認感測器已配對、啟用且知道 Server IP。只使用踏板／訓練台時不需要啟動 SDK Server。
3. 在專案根目錄開啟 PowerShell，安裝依賴並啟動：

   ```powershell
   uv sync
   uv run emg-cycling
   ```

4. 在「設定」填寫受試者 ID、姓名，勾選本次設備，測試 EMG Server 連線，並掃描／連接 P715 與 FTMS 訓練台。實際設備及 SDK Server 需在本機環境驗證。

命令列錄製工具仍可使用 `uv run emg-record --help`。測試指令為 `uv run python -m unittest discover -s tests -v`。

## 文件

- [系統使用操作說明](SYSTEM_USER_GUIDE.md)：基準測量、騎乘、兩次比較、歷史資料及報表操作。
- [資料取得、同步與分析方法](DATA_PIPELINE.md)：三種設備的欄位來源、時間對齊、計算公式、資料品質及限制。

