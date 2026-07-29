import json

import pytest

from fpl_engine.news import parse_structured_news, structured_news_schema


def test_structured_news_contract_is_strict_and_timezone_aware() -> None:
    payload = structured_news_schema()
    payload["evidence"][0]["source_player_id"] = "101"
    evidence = parse_structured_news(json.dumps(payload))

    assert len(evidence) == 1
    assert evidence[0].evidence_type == "injury"
    assert evidence[0].source_player_id == "101"
    assert evidence[0].evidence_at.utcoffset() is not None

    payload["evidence"][0]["invented_recommendation"] = "buy"
    with pytest.raises(ValueError, match="exactly"):
        parse_structured_news(json.dumps(payload))
