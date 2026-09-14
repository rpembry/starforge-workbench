from workbench.analytics import traffic_summary


def event(date, prop="1", name="site", **metrics):
    import json
    return {"source": "ga4-daily-collector", "details": json.dumps({
        "date": date, "property_id": prop, "property_name": name, **metrics
    })}


def test_latest_and_change_and_prompt():
    result = traffic_summary([
        event("2026-09-12", views=10, sessions=4, active_users=3, event_count=20),
        event("2026-09-13", views=2, sessions=2, active_users=2, event_count=5),
    ])
    report = result["reports"][0]
    assert report["date"] == "2026-09-13"
    assert report["changes"]["views"] == -8
    assert "what explains" in result["prompts"][0]


def test_ignores_other_sources_and_bad_details():
    assert traffic_summary([{"source": "other", "details": "{}"}, {"source": "ga4-daily-collector", "details": "bad"}]) == {"reports": [], "prompts": []}
