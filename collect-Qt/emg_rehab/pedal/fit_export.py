from __future__ import annotations
from pathlib import Path
from fit_tool import FitFileBuilder
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import Activity, Event, EventType, FileType, Manufacturer, Sport, SubSport

def exportPedalFit(records: list[dict], outputPath: Path) -> Path:
    if not records:
        raise ValueError("No pedal records were collected")
    rows = sorted(records, key=lambda row: float(row["timestamp"]))
    start = int(round(float(rows[0]["timestamp"]) * 1000))
    end = int(round(float(rows[-1]["timestamp"]) * 1000))
    duration = max(0.0, (end - start) / 1000)
    powers = [max(0, int(round(float(row["power_w"]))) ) for row in rows]
    cadenceValues = [float(row["cadence_rpm"]) for row in rows if row.get("cadence_rpm") is not None]

    fileId = FileIdMessage()
    fileId.type = FileType.ACTIVITY
    fileId.manufacturer = Manufacturer.DEVELOPMENT.value
    fileId.product = 0
    fileId.serial_number = 0
    fileId.time_created = start

    startEvent = EventMessage()
    startEvent.timestamp = start
    startEvent.event = Event.TIMER
    startEvent.event_type = EventType.START

    lastCadence: float | None = None
    fitRecords: list[RecordMessage] = []
    for row, power in zip(rows, powers):
        cadence = row.get("cadence_rpm")
        if cadence is not None:
            lastCadence = float(cadence)
        record = RecordMessage()
        record.timestamp = int(round(float(row["timestamp"]) * 1000))
        record.power = power
        if lastCadence is not None and power > 0:
            record.cadence = max(0, int(round(lastCadence)))
        left = row.get("left_balance_percent")
        if left is not None:
            # FIT stores the right percentage with bit 7 set.
            record.left_right_balance = 0x80 | max(0, min(100, int(round(100 - float(left)))))
        fitRecords.append(record)

    stopEvent = EventMessage()
    stopEvent.timestamp = end
    stopEvent.event = Event.TIMER
    stopEvent.event_type = EventType.STOP_ALL

    def summarize(message: LapMessage | SessionMessage) -> None:
        message.timestamp = end
        message.start_time = start
        message.total_elapsed_time = duration
        message.total_timer_time = duration
        message.total_work = int(round(sum(powers) * duration / max(1, len(powers))))
        message.avg_power = int(round(sum(powers) / len(powers)))
        message.max_power = max(powers)
        if cadenceValues:
            message.avg_cadence = int(round(sum(cadenceValues) / len(cadenceValues)))
            message.max_cadence = int(round(max(cadenceValues)))
        message.sport = Sport.CYCLING
        message.sub_sport = SubSport.INDOOR_CYCLING

    lap = LapMessage()
    lap.message_index = 0
    summarize(lap)
    lap.event = Event.LAP
    lap.event_type = EventType.STOP
    session = SessionMessage()
    session.message_index = 0
    summarize(session)
    session.first_lap_index = 0
    session.num_laps = 1
    session.event = Event.SESSION
    session.event_type = EventType.STOP
    activity = ActivityMessage()
    activity.timestamp = end
    activity.total_timer_time = duration
    activity.num_sessions = 1
    activity.type = Activity.MANUAL
    activity.event = Event.ACTIVITY
    activity.event_type = EventType.STOP

    outputPath.parent.mkdir(parents=True, exist_ok=True)
    builder = FitFileBuilder(auto_define=True, strict=True)
    builder.add(fileId)
    builder.add(startEvent)
    builder.add_all(fitRecords)
    builder.add(stopEvent)
    builder.add(lap)
    builder.add(session)
    builder.add(activity)
    builder.build().to_file(str(outputPath))
    return outputPath