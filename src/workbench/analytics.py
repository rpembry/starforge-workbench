"""Small, evidence-only views over GA4 collector observations."""

import json


SOURCE = "ga4-daily-collector"
METRICS = ("active_users", "sessions", "views", "event_count")


def _record(event):
    if event.get("source") != SOURCE:
        return None
    try:
        details = json.loads(event.get("details") or "{}")
        date = details["date"]
        property_id = str(details["property_id"])
        metrics = {name: int(details.get(name, 0)) for name in METRICS}
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None
    return {"property_id": property_id, "property_name": details.get("property_name", property_id),
            "date": date, **metrics, "occurred_at": event.get("occurred_at")}


def traffic_summary(events):
    """Return latest/prior daily totals and bounded prompts per property."""
    by_property = {}
    for event in events:
        record = _record(event)
        if record:
            by_property.setdefault(record["property_id"], []).append(record)

    reports, prompts = [], []
    for records in by_property.values():
        records.sort(key=lambda item: item["date"], reverse=True)
        latest = records[0]
        prior = records[1] if len(records) > 1 else None
        report = {key: latest[key] for key in ("property_id", "property_name", "date", *METRICS)}
        report["prior_date"] = prior["date"] if prior else None
        report["changes"] = {
            metric: (latest[metric] - prior[metric]) if prior else None for metric in METRICS
        }
        reports.append(report)
        label = latest["property_name"]
        if prior and prior["views"] and latest["views"] == 0:
            prompts.append(f"{label}: traffic fell to zero views on {latest['date']}; verify tracking and availability.")
        elif prior and prior["views"] and abs(latest["views"] - prior["views"]) / prior["views"] >= 0.5:
            prompts.append(f"{label}: what explains the {latest['views'] - prior['views']:+d} view change from {prior['date']} to {latest['date']}?")
        if latest["event_count"] > latest["sessions"] * 20 and latest["sessions"]:
            prompts.append(f"{label}: event volume is unusually high relative to sessions; are enhanced events intentional?")
    reports.sort(key=lambda item: item["property_name"].lower())
    return {"reports": reports, "prompts": prompts[:8]}
