# Trigno SDK Server EMG TCP 測試

這個小程式使用 Python 標準函式庫連接 Trigno SDK Server，不需要安裝額外套件。

## 使用前確認

1. 開啟 Trigno SDK Server。
2. 在 Server 中完成感測器配對並確認感測器為啟用狀態。
3. 確認 Server 顯示的連接埠是 Command `50040`、EMG `50041`、AUX `50042`。
4. 關閉其他正在控制或擷取同一台 Trigno 的程式。

程式雖然只儲存 EMG，但仍會連接 AUX `50042`。SDK Server 3.x 通常要兩個資料 socket 都連上後才接受 `START`。

## 執行

此電腦可用 Windows Python Launcher：

```powershell
py trigno_emg.py --duration 10 --output emg_data.csv
```

預設設定為 `127.0.0.1`、2000 Hz、16 個 EMG 通道。若 Server 在另一台電腦：

```powershell
py trigno_emg.py --host 192.168.1.100 --duration 10
```

程式會顯示 Server 版本、配對的感測器編號、擷取進度，以及每個通道的 min / max / RMS。CSV 每列是一個取樣點，欄位為時間及 16 個 EMG float32 值。

若 Server 回報沒有已配對感測器或所有通道的 RMS 都是 0，請回到 SDK Server 完成 Pair/Enable；TCP 連線成功本身不代表感測器已配對。

## Qt 即時監看介面

介面顯示 16 個感測器通道的狀態、最新值、最近一秒 RMS，以及可切換通道的即時波形。勾選「儲存 CSV」即可保留完整資料。

波形預設採固定 `±1.0 mV` 垂直範圍，避免微小雜訊被自動放大；可在介面調整為 `±0.05` 至 `±20 mV`。表格與波形以 mV 顯示，CSV 仍保存 SDK Server 原始的 V 值。

```powershell
py delsys_gui.py
```

若使用 uv，建議直接由 lockfile 建立環境並啟動：

```powershell
uv sync
uv run python delsys_gui.py
```

專案已將 Windows AMD64 設為 uv 必須支援的平台，避免解析到沒有 Windows wheel 的 `PyQt5-Qt5` 版本。

若環境尚未安裝 GUI 套件：

```powershell
py -m pip install PyQt5 pyqtgraph numpy
```

按「連線並開始」會同時連上 Command `50040`、EMG `50041`、AUX `50042`，再送出 `START`。關閉視窗或按「停止」都會向 Server 送出 `STOP`。

## 目前量測精度如何理解

- TCP 輸出間隔：目前 SDK Server 為 Backwards Compatibility ON 且 UPSAMPLING ON，legacy EMG `50041` 輸出 2000 Hz，即每 0.5 ms 一筆。
- 感測器原生取樣：目前配對在 Slot 1 的 Type O 感測器回報 1925.9259 Hz（26 筆 / 13.5 ms），原生間隔約 0.51923 ms。Server 的 2000 Hz 是重取樣結果，不會增加原始時間解析度。
- 傳輸數值：每個通道以 IEEE 754 float32 傳輸，約有 7 位十進位有效數字；介面也顯示到 7 位有效數字。
- 感測器解析度：float32 是傳輸格式，不代表硬體有 32-bit 量測解析度。真正解析度取決於感測器型號、量測範圍與 mode；常見 Trigno EMG mode 為 16-bit。
- 實際有效精度：還會受到感測器本底雜訊、皮膚處理、電極位置、動作偽影與無線品質影響，通常不應只用小數位數判斷。

若要計算特定硬體的理論最小電壓步階，可用 `完整輸入範圍 / 2^解析位元數`；需要先確認感測器型號及目前 mode 的輸入範圍。

## Delsys API key / license

目前 TCP SDK Server 與 DelsysAPI/AeroPy 是兩條不同的連線方式：

- SDK Server TCP：透過 `50040`～`50042`，不使用 API key/license。
- DelsysAPI/AeroPy：程式直接控制 USB 基地台，初始化時呼叫 `ValidateBase(key, license)`。

專案中的 `DelsysAPI.key` 和 `DelsysAPI.lic` 已加入 `.gitignore`。`delsys_aeropy.py` 會從檔案載入內容，不會把憑證寫死在原始碼中。要使用 AeroPy，還需把廠商 API 套件中的 `DelsysAPI.dll` 及其相依檔放入 `resources/`，並安裝 `pythonnet`；之後可用：

```python
from delsys_aeropy import connect_aeropy

api = connect_aeropy()
api.ScanSensors(False, []).Result
api.SelectAllSensors()
api.Configure()
api.Start()
```

同一時間請只讓 SDK Server TCP 或 AeroPy 其中一個後端控制基地台，避免 USB 裝置占用衝突。
"# emg-cycling" 
