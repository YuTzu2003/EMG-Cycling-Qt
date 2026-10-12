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

## Record from the command line

```powershell
uv run emg-record --help
```
