from workbench.analytics import traffic_summary


def event(date, prop="1", name="site", **metrics):
    metrics = dict(active_users=0, sessions=0, views=0, event_count=0, **{}) | metrics
    import json
    return {"source": "ga4-daily-collector", "details": json.dumps({
        "date": date, "property_id": prop, "property_name": name, **metrics
    })}


def test_latest_and_change_and_prompt():
    from datetime import datetime, timedelta, timezone
    today=datetime.now(timezone.utc).date()
    prior=(today-timedelta(days=1)).isoformat()
    latest=today.isoformat()
    result = traffic_summary([
        event(prior, views=10, sessions=4, active_users=3, event_count=20),
        event(latest, views=2, sessions=2, active_users=2, event_count=5),
    ])
    report = result["reports"][0]
    assert report["date"] == latest
    assert report["changes"]["views"] == -8
    assert "what explains" in result["prompts"][0]


def test_ignores_other_sources_and_bad_details():
    assert traffic_summary([{"source": "other", "details": "{}"}, {"source": "ga4-daily-collector", "details": "bad"}]) == {"reports": [], "prompts": []}


def test_malformed_metrics_and_shapes_do_not_crash_or_become_zero():
    import json
    invalid = [None, [], 42, {'date':'bad'}, {'date':'2026-09-12','property_id':'1'}]
    good = json.loads(event('2026-09-12')['details'])
    for metric in [None, True, -1, 1.5, float('inf'), [], 'bad']:
        invalid.append(dict(good, views=metric))
    invalid.append(dict(good, property_id=[]))
    invalid.append(dict(good, date=[]))
    records = [{'source':'ga4-daily-collector','details':json.dumps(item)} for item in invalid]
    assert traffic_summary(records) == {'reports':[], 'prompts':[]}
    assert len(traffic_summary(records+[event('2026-09-12',name=[])])['reports'])==1


def test_duplicate_days_and_old_data_are_not_fresh_comparisons():
    old=event('2020-01-01',views=5)
    corrected=event('2020-01-01',views=8)
    old['occurred_at']='2020-01-02T00:00:00+00:00'
    corrected['occurred_at']='2020-01-03T00:00:00+00:00'
    result=traffic_summary([old,corrected,event('2019-12-31',views=3)])
    report=result['reports'][0]
    assert report['views']==8 and report['changes']['views']==5
    assert report['prior_date']=='2019-12-31' and report['stale']
    assert 'refresh collection' in result['prompts'][0]
