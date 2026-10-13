"""Small, evidence-only views over GA4 collector observations."""

import json
from datetime import date as calendar_date, datetime, timezone


SOURCE = "ga4-daily-collector"
METRICS = ("active_users", "sessions", "views", "event_count")


def _record(event):
    if event.get("source") != SOURCE:
        return None
    try:
        details = json.loads(event.get("details") or "{}")
        if not isinstance(details, dict):
            return None
        date = details['date']
        if not isinstance(date, str) or calendar_date.fromisoformat(date).isoformat() != date:
            return None
        if calendar_date.fromisoformat(date) > datetime.now(timezone.utc).date():
            return None
        property_id = details['property_id']
        if isinstance(property_id, bool) or not isinstance(property_id, (str, int)):
            return None
        property_id = str(property_id)
        if not property_id.isascii() or not property_id.isdigit() or len(property_id) > 100:
            return None
        metrics = {}
        for name in METRICS:
            value = details[name]
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10**15:
                return None
            metrics[name] = value
        label = details.get('property_name', property_id)
        if not isinstance(label, str) or not label.strip() or len(label) > 200:
            label = property_id
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None
    return {'property_id': property_id, 'property_name': label,
            'date': date, **metrics, 'occurred_at': str(event.get('occurred_at') or '')}


def traffic_summary(events):
    """Return latest/prior daily totals and bounded prompts per property."""
    by_property = {}
    for event in events:
        record = _record(event)
        if record:
            by_property.setdefault(record["property_id"], []).append(record)

    reports, prompts = [], []
    for records in by_property.values():
        # Compare distinct days, choosing the most recently recorded correction per day.
        records.sort(key=lambda item: (item['date'], item['occurred_at']), reverse=True)
        records = list({item['date']: item for item in reversed(records)}.values())
        records.sort(key=lambda item: item['date'], reverse=True)
        latest = records[0]
        prior = records[1] if len(records) > 1 else None
        report = {key: latest[key] for key in ("property_id", "property_name", "date", *METRICS)}
        report["prior_date"] = prior["date"] if prior else None
        report["changes"] = {
            metric: (latest[metric] - prior[metric]) if prior else None for metric in METRICS
        }
        report['stale'] = (datetime.now(timezone.utc).date() - calendar_date.fromisoformat(latest['date'])).days > 2
        reports.append(report)
        if report['stale']:
            prompts.append(f"{latest['property_name']}: latest traffic evidence is from {latest['date']}; refresh collection before interpreting changes.")
            continue
        label = latest["property_name"]
        if prior and prior["views"] and latest["views"] == 0:
            prompts.append(f"{label}: traffic fell to zero views on {latest['date']}; verify tracking and availability.")
        elif prior and prior["views"] and abs(latest["views"] - prior["views"]) / prior["views"] >= 0.5:
            prompts.append(f"{label}: what explains the {latest['views'] - prior['views']:+d} view change from {prior['date']} to {latest['date']}?")
        if latest["event_count"] > latest["sessions"] * 20 and latest["sessions"]:
            prompts.append(f"{label}: event volume is unusually high relative to sessions; are enhanced events intentional?")
    reports.sort(key=lambda item: item["property_name"].lower())
    return {"reports": reports, "prompts": prompts[:8]}
