## EMG Cycling Monitor

復健騎乘測試桌面程式，整合 Delsys EMG、阻力計與踏板資料，提供即時收集、RMS 分析、踩踏角度分析、資料庫儲存與歷次比較。

### 功能

- 即時收集 EMG、阻力計與踏板資料。
- 顯示肌肉 RMS、功率、踏頻與左右平衡。
- 匯入 CSV 進行踩踏角度與肌肉活化分析。
- 儲存測試資料至 SQL Server，並比較歷次 RMS。
- 匯出 HTML 分析報表。

### 環境需求

- Windows 10 或 11
- Python 3.12
- Delsys Trigno SDK Server
- Bluetooth LE 阻力計或踏板（選用）
- [uv](https://docs.astral.sh/uv/)

### 安裝 Delsys Trigno SDK

本程式透過 Trigno SDK Server 接收 EMG。請從 Delsys 官方 [Trigno SDK 支援頁](https://delsys.com/support/trigno-sdk/) 下載並安裝 **Delsys SDK Server Installer**；安裝後啟動 Trigno Control Utility，確認 Base Station 與感測器已連線，再將 Server 位址填入程式的 EMG 設定。

### 啟動

```powershell
uv sync
uv run main.py
```

### 基本流程

1. 在「設定」填寫病人資料。
2. 需要資料庫時，先測試 SQL Server 連線並選擇資料庫。
3. 設定 EMG Server，連線阻力計與踏板。
4. 到「收集資料」開始或結束測試。
5. 在「本次摘要」檢視肌肉、踏板與踩踏角度分析，或匯出 HTML 報表。

詳細操作請見 [系統使用說明](docs/SYSTEM_USER_GUIDE.md)。
