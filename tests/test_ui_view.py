from fpl_engine.ui.view import pitch_html


def test_pitch_view_escapes_player_names_and_marks_roles() -> None:
    lookup = {
        "1": {
            "web_name": "<Captain>",
            "team_short_name": "NTH",
            "position": "FWD",
            "price_tenths": 75,
        },
        "2": {
            "web_name": "Keeper",
            "team_short_name": "STH",
            "position": "GK",
            "price_tenths": 45,
        },
    }
    content = pitch_html(
        (
            {"source_player_id": "1", "is_starter": True, "bench_order": None},
            {"source_player_id": "2", "is_starter": False, "bench_order": 1},
        ),
        lookup,
        captain_id="1",
        vice_captain_id=None,
    )

    assert "&lt;Captain&gt;" in content
    assert "<Captain>" not in content
    assert '<span class="fpl-role">C</span>' in content
    assert "£7.5m" in content
    assert "Bench" in content
