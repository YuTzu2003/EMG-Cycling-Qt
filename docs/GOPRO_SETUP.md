# GoPro recording integration

## Lower-latency start and video download

After **配對並測試 GoPro** succeeds, the application keeps that BLE session
open and sends an Open GoPro keep-alive every three seconds. When collection
starts, it can send the shutter command through that already-open session
instead of reconnecting and pairing again. This reduces the software-side
delay, but is not a hardware trigger: use a visible/audible cue when you need
frame-accurate alignment.

Stopping a collection sends the shutter-off command but keeps the verified
Bluetooth session ready for the next collection. Use **中斷 GoPro** (or close
the application) to release that session.

To download a video:

1. Stop the camera recording.
2. Press **下載最新影片** in Settings, or **選擇影片下載** to select an MP4
   from the camera list.

The app reads the GoPro Wi-Fi SSID and password during Bluetooth verification,
enables the camera access point, then asks Windows to join it automatically
before reading or downloading media. The password stays only in application
memory and is never added to the event log or session files. The file is saved
directly in the same data output folder selected on the collection page. If a
selected file already exists, the app asks whether to overwrite, rename, or
skip it. The app does not delete camera files.

The collection page now has a **GoPro 影片** checkbox. When selected, the
application discovers a nearby camera over Bluetooth and sends the Open GoPro
start/stop recording commands when a collection starts and ends. The Settings
page test performs a real Open GoPro command/response check without recording.

## Before a recording

1. Turn on the GoPro and enable Bluetooth / wireless connections.
2. Pair the camera with Windows if the camera prompts for pairing.
3. Keep the camera nearby. The first command scans for a Bluetooth name that
   contains `GoPro`; its address is saved for the next collection.
4. Select **GoPro 影片** on the collection page, then start the normal EMG
   collection.

The event log records the camera name, Bluetooth address, and start/stop
timestamps. These timestamps are also attached to the in-memory ride session
to support later video/EMG alignment.

## Scope and limitations

Video download is a separate, explicit action. When requested, it temporarily
changes Windows to the GoPro Wi-Fi access point so the media HTTP API can be
reached; do not start a download while network access is required for another
task.

Bluetooth timing is not a hardware trigger. Validate the offset for any
experiment needing frame-level synchronization; a visible or audible sync cue
at recording start is recommended.

## Status, compatibility, and troubleshooting

GoPro is optional: leaving **GoPro 影片** unchecked, not configuring a camera,
or losing its control connection never stops EMG, trainer, or pedal capture.

The upper-right indicator has deliberately conservative meanings:

- **未連線**: no recent successful control verification, or the last command failed.
- **連線中**: a Bluetooth control session is being established.
- **控制已驗證**: Bluetooth pairing and the control-setup commands completed.
- **錄影指令已送出（未確認）**: the camera acknowledged the BLE write. Confirm
  the red dot / timer on the camera itself before treating this as a recording.

The event log records the selected Bluetooth address, the standard BLE camera
model and firmware fields when the camera exposes them, each command step, and
any camera command-response packet. A GATT acknowledgement alone is not a
camera recording confirmation: GoPro model and firmware support for pairing,
third-party control, and command responses differs.

The session metadata includes the Bluetooth address, available model/firmware
details, and local start/stop command timestamps for coarse alignment. This
integration records a downloaded local video path after a user-initiated
download, but cannot guarantee that it belongs to a particular collection.
For frame-level
synchronization, use a visible or audible cue and measure the offset.

If recording does not start, include the **Camera model**, **Firmware
revision**, and all `GoPro |` event-log lines when reporting the problem. Make
sure the camera can record normally, is in video mode, is awake, is not in USB
mode, and is not being controlled by GoPro Quik or another application.

For HERO11 Black, select **影片** mode directly on the camera before starting
the session. The application sends the official Open GoPro shutter command to
the active mode and deliberately does not change the camera's chosen preset.
