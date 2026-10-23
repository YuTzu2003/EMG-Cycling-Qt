"""Small, accessible SVG visualizations for the web analysis pages."""
from __future__ import annotations

from html import escape
from math import isfinite

from markupsafe import Markup


def angle_profile_charts(result: dict) -> Markup:
    profiles = result["profiles"]
    summary = result["summary"]
    cards = []
    for channel in summary["channel"]:
        data = profiles[(profiles["channel"] == channel) & profiles["phase"].isin(["start", "end"])]
        if data.empty:
            continue
        ymax = max((float(value) for value in data["mean_percent"] if isfinite(float(value))), default=1) or 1
        lines = []
        for phase, color, name in (("start", "#1479df", "前段"), ("end", "#ed5a52", "後段")):
            phase_data = data[data["phase"] == phase].sort_values("angle_bin_center")
            points = " ".join(
                f"{38 + float(row.angle_bin_center) * 680 / 360:.1f},{170 - float(row.mean_percent) * 132 / ymax:.1f}"
                for row in phase_data.itertuples() if isfinite(float(row.mean_percent)))
            lines.append(f'<polyline fill="none" stroke="{color}" stroke-width="3" points="{points}"/>'
                         f'<text x="{620 if phase == "start" else 674}" y="25" fill="{color}">{name}</text>')
        label = summary.loc[summary["channel"] == channel, "location_zh"].iloc[0]
        cards.append('<figure class="profile-card"><figcaption>' + escape(str(label)) + '</figcaption>'
                     '<svg viewBox="0 0 760 205" role="img" aria-label="前後段肌電角度曲線">'
                     '<line x1="38" y1="170" x2="718" y2="170" stroke="#abb2bb"/>'
                     '<line x1="38" y1="36" x2="38" y2="170" stroke="#abb2bb"/>'
                     '<text x="40" y="195">0°</text><text x="680" y="195">360°</text>'
                     + "".join(lines) + '</svg></figure>')
    return Markup("".join(cards))
