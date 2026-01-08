#!/usr/bin/env python3
import argparse
import json
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch tenant devices from the Smart Nicosia API."
    )
    parser.add_argument(
        "--base-url",
        default="https://nokia.smartnicosia.eu/backend/openapi/getTenantDevices",
        help="Base endpoint URL (without query string).",
    )
    parser.add_argument(
        "--entity-type",
        default="TENANT",
        help="Entity type for the query (default: TENANT).",
    )
    parser.add_argument(
        "--entity-name",
        default="CYTA",
        help="Entity name for the query (default: CYTA).",
    )
    parser.add_argument(
        "--accept",
        default="application/json",
        help="Accept header value (default: application/json).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Request timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON if possible.",
    )
    parser.add_argument(
        "--unique-device-type",
        action="store_true",
        help="Print one example per device_type (first seen).",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification (insecure).",
    )
    parser.add_argument(
        "--cafile",
        help="Path to a CA bundle file for TLS verification.",
    )
    parser.add_argument(
        "--save-examples",
        help="Directory to save example payloads as JSON files.",
    )
    return parser.parse_args(argv)


def build_url(base_url: str, params: Dict[str, str]) -> str:
    query = urllib.parse.urlencode(params)
    return f"{base_url}?{query}"


def normalize_params(params: Dict[str, Optional[object]]) -> Dict[str, str]:
    normalized: Dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        normalized[key] = str(value)
    return normalized


def fetch(
    url: str,
    accept: str,
    timeout: float,
    insecure: bool,
    cafile: Optional[str],
) -> str:
    if insecure:
        context = ssl._create_unverified_context()
    elif cafile:
        context = ssl.create_default_context(cafile=cafile)
    else:
        context = ssl.create_default_context()

    req = urllib.request.Request(url, headers={"accept": accept}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
        return resp.read().decode("utf-8")


def parse_response(body: str) -> Any:
    return json.loads(body)


def get_json(
    base_url: str,
    params: Dict[str, str],
    accept: str = "application/json",
    timeout: float = 30.0,
    insecure: bool = False,
    cafile: Optional[str] = None,
) -> Any:
    url = build_url(base_url, params)
    body = fetch(url, accept, timeout, insecure, cafile)
    return parse_response(body)


def get_devices(
    base_url: str,
    entity_type: str,
    entity_name: str,
    accept: str = "application/json",
    timeout: float = 30.0,
    insecure: bool = False,
    cafile: Optional[str] = None,
) -> Any:
    params = normalize_params({"entityType": entity_type, "entityName": entity_name})
    return get_json(base_url, params, accept, timeout, insecure, cafile)


def get_alarms(
    entity_name: str,
    entity_type: str = "DEVICE",
    limit: int = 10,
    state: Optional[str] = None,
    base_url: str = "https://nokia.smartnicosia.eu/backend/openapi/getAlarms",
    accept: str = "application/json",
    timeout: float = 30.0,
    insecure: bool = False,
    cafile: Optional[str] = None,
) -> Any:
    params = normalize_params(
        {
            "entityType": entity_type,
            "entityName": entity_name,
            "limit": limit,
            "state": state,
        }
    )
    return get_json(base_url, params, accept, timeout, insecure, cafile)


def get_latest_telemetry(
    entity_name: str,
    entity_type: str = "DEVICE",
    base_url: str = "https://nokia.smartnicosia.eu/backend/openapi/getLatestTelemetry",
    accept: str = "application/json",
    timeout: float = 30.0,
    insecure: bool = False,
    cafile: Optional[str] = None,
    keys: Optional[str] = None,
) -> Any:
    params = normalize_params(
        {"entityType": entity_type, "entityName": entity_name, "keys": keys}
    )
    return get_json(base_url, params, accept, timeout, insecure, cafile)


def get_latest_attribute(
    entity_name: str,
    entity_type: str = "DEVICE",
    base_url: str = "https://nokia.smartnicosia.eu/backend/openapi/getLatestAttribute",
    accept: str = "application/json",
    timeout: float = 30.0,
    insecure: bool = False,
    cafile: Optional[str] = None,
) -> Any:
    params = normalize_params({"entityType": entity_type, "entityName": entity_name})
    return get_json(base_url, params, accept, timeout, insecure, cafile)


def get_telemetry_range(
    entity_name: str,
    start_ts: int,
    end_ts: int,
    entity_type: str = "DEVICE",
    keys: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    base_url: str = "https://nokia.smartnicosia.eu/backend/openapi/getTelemetryRange",
    accept: str = "application/json",
    timeout: float = 30.0,
    insecure: bool = False,
    cafile: Optional[str] = None,
) -> Any:
    params = normalize_params(
        {
            "entityType": entity_type,
            "entityName": entity_name,
            "startTs": start_ts,
            "endTs": end_ts,
            "keys": keys,
            "username": username,
            "password": password,
        }
    )
    return get_json(base_url, params, accept, timeout, insecure, cafile)


def get_object_types(
    base_url: str = "https://nokia.smartnicosia.eu/backend/openapi/object_types",
    accept: str = "application/json",
    timeout: float = 30.0,
    insecure: bool = False,
    cafile: Optional[str] = None,
) -> Any:
    return get_json(base_url, {}, accept, timeout, insecure, cafile)


def get_analytics(
    base_url: str = "https://nokia.smartnicosia.eu/backend/openapi/analytics",
    accept: str = "application/json",
    timeout: float = 30.0,
    insecure: bool = False,
    cafile: Optional[str] = None,
) -> Any:
    return get_json(base_url, {}, accept, timeout, insecure, cafile)


def get_traffic_reports(
    va_ids: str,
    group_by: str,
    start_date: int,
    end_date: int,
    base_url: str = "https://nokia.smartnicosia.eu/backend/openapi/getTrafficReports",
    accept: str = "application/json",
    timeout: float = 30.0,
    insecure: bool = False,
    cafile: Optional[str] = None,
) -> Any:
    params = normalize_params(
        {
            "va_ids": va_ids,
            "group_by": group_by,
            "start_date": start_date,
            "end_date": end_date,
        }
    )
    return get_json(base_url, params, accept, timeout, insecure, cafile)


def get_general_reports(
    group_by: str,
    line_type: str,
    start_date: int,
    end_date: int,
    base_url: str = "https://nokia.smartnicosia.eu/backend/openapi/generalReports",
    accept: str = "application/json",
    timeout: float = 30.0,
    insecure: bool = False,
    cafile: Optional[str] = None,
) -> Any:
    params = normalize_params(
        {
            "group_by": group_by,
            "line_type": line_type,
            "start_date": start_date,
            "end_date": end_date,
        }
    )
    return get_json(base_url, params, accept, timeout, insecure, cafile)


def get_hourly_reports(
    week: int,
    year: int,
    base_url: str = "https://nokia.smartnicosia.eu/backend/openapi/hourlyReports",
    accept: str = "application/json",
    timeout: float = 30.0,
    insecure: bool = False,
    cafile: Optional[str] = None,
) -> Any:
    params = normalize_params({"week": week, "year": year})
    return get_json(base_url, params, accept, timeout, insecure, cafile)


def get_example_payloads(
    insecure: bool = False,
    cafile: Optional[str] = None,
    tenant_name: str = "CYTA",
    device_name: str = "YL1015",
    alarm_device_name: str = "YW1394",
    traffic_group_by: str = "day",
    line_type: str = "AVERAGE_SPEED",
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    start = int((now - timedelta(days=1)).timestamp() * 1000)
    end = int(now.timestamp() * 1000)
    iso = now.isocalendar()

    return {
        "getTenantDevices": get_devices(
            base_url="https://nokia.smartnicosia.eu/backend/openapi/getTenantDevices",
            entity_type="TENANT",
            entity_name=tenant_name,
            insecure=insecure,
            cafile=cafile,
        ),
        "getLatestTelemetry": get_latest_telemetry(
            entity_name=device_name,
            insecure=insecure,
            cafile=cafile,
        ),
        "getLatestAttribute": get_latest_attribute(
            entity_name=device_name,
            insecure=insecure,
            cafile=cafile,
        ),
        "getTelemetryRange": get_telemetry_range(
            entity_name=device_name,
            start_ts=start,
            end_ts=end,
            insecure=insecure,
            cafile=cafile,
        ),
        "getAlarms": get_alarms(
            entity_name=alarm_device_name,
            limit=10,
            insecure=insecure,
            cafile=cafile,
        ),
        "object_types": get_object_types(insecure=insecure, cafile=cafile),
        "analytics": get_analytics(insecure=insecure, cafile=cafile),
        "getTrafficReports": get_traffic_reports(
            va_ids="[]",
            group_by=traffic_group_by,
            start_date=start,
            end_date=end,
            insecure=insecure,
            cafile=cafile,
        ),
        "generalReports": get_general_reports(
            group_by="ONE_HOUR",
            line_type=line_type,
            start_date=start,
            end_date=end,
            insecure=insecure,
            cafile=cafile,
        ),
        "hourlyReports": get_hourly_reports(
            week=iso.week,
            year=iso.year,
            insecure=insecure,
            cafile=cafile,
        ),
    }


def save_example_payloads(
    output_dir: str,
    insecure: bool = False,
    cafile: Optional[str] = None,
    tenant_name: str = "CYTA",
    device_name: str = "YL1015",
    alarm_device_name: str = "YW1394",
    traffic_group_by: str = "day",
    line_type: str = "AVERAGE_SPEED",
    pretty: bool = True,
) -> Dict[str, str]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    examples = get_example_payloads(
        insecure=insecure,
        cafile=cafile,
        tenant_name=tenant_name,
        device_name=device_name,
        alarm_device_name=alarm_device_name,
        traffic_group_by=traffic_group_by,
        line_type=line_type,
    )

    paths: Dict[str, str] = {}
    for name, payload in examples.items():
        file_path = output_path / f"{name}.json"
        file_path.write_text(to_json(payload, pretty), encoding="utf-8")
        paths[name] = str(file_path)
    return paths


def to_json(data: Any, pretty: bool) -> str:
    if pretty:
        return json.dumps(data, indent=2)
    return json.dumps(data)


def iter_unique_by_device_type(items: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    seen: set[str] = set()
    unique_items: list[dict] = []
    for item in items:
        device_type = item.get("device_type")
        if not device_type or device_type in seen:
            continue
        seen.add(device_type)
        unique_items.append(item)
    return unique_items


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    if args.save_examples:
        paths = save_example_payloads(
            output_dir=args.save_examples,
            insecure=args.insecure,
            cafile=args.cafile,
        )
        for name, path in paths.items():
            print(f"{name}: {path}")
        return 0

    params = {"entityType": args.entity_type, "entityName": args.entity_name}
    url = build_url(args.base_url, params)
    body = fetch(url, args.accept, args.timeout, args.insecure, args.cafile)

    try:
        data = parse_response(body)
    except json.JSONDecodeError:
        print(body)
        return 0

    if args.unique_device_type and isinstance(data, list):
        data = iter_unique_by_device_type(data)

    print(to_json(data, args.pretty))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
