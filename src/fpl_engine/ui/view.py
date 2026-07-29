"""Framework-independent presentation helpers."""

from __future__ import annotations

import html
from collections.abc import Iterable, Mapping
from typing import Any

POSITION_ORDER = ("GK", "DEF", "MID", "FWD")


def pitch_html(
    entries: Iterable[Mapping[str, Any]],
    player_lookup: Mapping[str, Mapping[str, Any]],
    captain_id: str | None,
    vice_captain_id: str | None,
) -> str:
    """Return a compact responsive pitch and bench view."""

    entry_rows = list(entries)
    starters = [entry for entry in entry_rows if entry["is_starter"]]
    substitutes = sorted(
        (entry for entry in entry_rows if not entry["is_starter"]),
        key=lambda entry: entry.get("bench_order") or 99,
    )
    pitch_rows = "".join(
        _row(
            position,
            (
                entry
                for entry in starters
                if player_lookup[str(entry["source_player_id"])]["position"]
                == position
            ),
            player_lookup,
            captain_id,
            vice_captain_id,
        )
        for position in POSITION_ORDER
    )
    bench = _row(
        "Bench",
        substitutes,
        player_lookup,
        captain_id,
        vice_captain_id,
    )
    return f"""
    <style>
      .fpl-pitch {{
        background: linear-gradient(145deg, #087f5b, #0a5c47);
        border: 1px solid #4ade80;
        border-radius: 18px;
        padding: 18px 10px;
        box-shadow: inset 0 0 0 3px rgba(255,255,255,.08);
      }}
      .fpl-row {{ margin: 6px 0 15px; text-align: center; }}
      .fpl-row-label {{
        color: #d1fae5; font-size: 11px; font-weight: 700;
        letter-spacing: .08em; text-transform: uppercase;
      }}
      .fpl-players {{
        display: flex; flex-wrap: wrap; gap: 8px;
        justify-content: center; margin-top: 5px;
      }}
      .fpl-player {{
        background: #ffffff; color: #102a43; border-radius: 8px;
        min-width: 88px; max-width: 128px; padding: 7px 8px;
        box-shadow: 0 2px 8px rgba(0,0,0,.18);
      }}
      .fpl-name {{ font-size: 13px; font-weight: 750; }}
      .fpl-team {{ color: #52606d; font-size: 10px; margin-top: 2px; }}
      .fpl-role {{
        background: #5b21b6; color: white; border-radius: 999px;
        display: inline-block; font-size: 9px; margin-left: 4px; padding: 1px 5px;
      }}
      .fpl-bench {{
        background: #172554; border-radius: 14px; margin-top: 10px;
        padding: 12px 8px 2px;
      }}
      @media (max-width: 640px) {{
        .fpl-player {{ min-width: 70px; max-width: 96px; padding: 6px 5px; }}
        .fpl-name {{ font-size: 11px; }}
      }}
    </style>
    <div class="fpl-pitch">{pitch_rows}</div>
    <div class="fpl-bench">{bench}</div>
    """


def _row(
    label: str,
    entries: Iterable[Mapping[str, Any]],
    player_lookup: Mapping[str, Mapping[str, Any]],
    captain_id: str | None,
    vice_captain_id: str | None,
) -> str:
    cards = []
    for entry in entries:
        source_id = str(entry["source_player_id"])
        player = player_lookup[source_id]
        role = ""
        if source_id == captain_id:
            role = '<span class="fpl-role">C</span>'
        elif source_id == vice_captain_id:
            role = '<span class="fpl-role">VC</span>'
        cards.append(
            '<div class="fpl-player">'
            f'<div class="fpl-name">{html.escape(str(player["web_name"]))}{role}</div>'
            f'<div class="fpl-team">{html.escape(str(player["team_short_name"]))} · '
            f'£{int(player["price_tenths"]) / 10:.1f}m</div>'
            "</div>"
        )
    return (
        '<div class="fpl-row">'
        f'<div class="fpl-row-label">{html.escape(label)}</div>'
        f'<div class="fpl-players">{"".join(cards)}</div>'
        "</div>"
    )
