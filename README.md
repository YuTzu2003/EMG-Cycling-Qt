# EMG Cycling Monitor

Desktop application for collecting Delsys Trigno EMG data and controlling an
FTMS smart trainer.

## Start the GUI

```powershell
uv sync
uv run emg-cycling
```

Before collecting, pair and enable the required sensors in Trigno Control
Utility, then ensure the SDK Server Command, EMG, and AUX ports are available.

## Body-muscle map

The collection page provides an interactive anterior/posterior muscle map.
Configured sensors are shown on the matching muscle and side from
`sensor_mapping.json`:

- Click a muscle or its sensor legend to show or hide that sensor's live EMG waveform.
- Enabled Trigno sensors are indicated with a green outline.
- The front/back switch keeps the selected waveform sensors synchronized.

The third-party GitHub references and their use are recorded in
`GITHUB_REFERENCES.txt`.

## Code structure

- `src/delsys/emg/`: Trigno SDK protocol, sensor mapping, and EMG worker threads.
- `src/delsys/resistance/`: FTMS trainer communication and resistance telemetry CSV recording.
- `src/delsys/collection.py`: subject details and collection metadata shared by both data sources.
- `src/delsys/gui.py`: PyQt interface coordinating collection and visualization.

## Command-line recording

```powershell
uv run emg-record --host 127.0.0.1 --duration 10 --output recordings\trial_01.csv
```
