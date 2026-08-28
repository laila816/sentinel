"""Project Sentinel: end-to-end NEO acquisition, cleaning and triage pipeline."""

from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path
from statistics import median
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"
IDS_PATH = RAW_DIR / "extracted_ids.txt"
GROUND_LOG_PATH = RAW_DIR / "ground_station_log.csv"
OUTPUT_PATH = PROCESSED_DIR / "clean_data.csv"

API_URL = "https://api.nasa.gov/neo/rest/v1/feed"
SCRAPE_URL = (
    "https://science.nasa.gov/science-research/planetary-science/"
    "planetary-defense/near-earth-asteroids/"
)

SIZE_THRESHOLD_KM = 0.14
DISTANCE_THRESHOLD_LD = 10.0
REQUIRED_LOG_FIELDS = ["neo_id", "station_name", "confidence_score", "tracking_status"]


def safe_float(value: Any, default: float | None = None) -> float | None:
    """Convert a value to float without allowing malformed input to crash the pipeline."""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def get_date_windows() -> list[tuple[str, str]]:
    """Return two adjacent seven-day windows ending yesterday."""
    from datetime import date, timedelta

    end = date.today() - timedelta(days=1)
    first_start = end - timedelta(days=13)
    first_end = first_start + timedelta(days=6)
    second_start = first_end + timedelta(days=1)
    second_end = end
    return [
        (first_start.isoformat(), first_end.isoformat()),
        (second_start.isoformat(), second_end.isoformat()),
    ]


def fetch_neo_records(api_key: str, windows: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Fetch NEO records from all requested NASA feed windows."""
    all_records: list[dict[str, Any]] = []

    for start_date, end_date in windows:
        params = {
            "start_date": start_date,
            "end_date": end_date,
            "api_key": api_key,
        }
        try:
            response = requests.get(API_URL, params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(
                f"NASA API request failed for {start_date} to {end_date}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("NASA returned a response that is not valid JSON.") from exc

        for _, records_for_date in payload.get("near_earth_objects", {}).items():
            if isinstance(records_for_date, list):
                all_records.extend(
                    record for record in records_for_date if isinstance(record, dict)
                )

    return all_records


def extract_unique_ids(all_records: list[dict[str, Any]]) -> list[str]:
    """Extract NEO reference IDs, preserving first-seen order."""
    neo_ids = [
        str(record["neo_reference_id"])
        for record in all_records
        if record.get("neo_reference_id") is not None
    ]
    return list(dict.fromkeys(neo_ids))


def write_extracted_ids(neo_ids: list[str]) -> None:
    """Write the IDs used to create the internal ground-station log."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    IDS_PATH.write_text("\n".join(neo_ids), encoding="utf-8")


def select_close_approach(record: dict[str, Any]) -> dict[str, Any] | None:
    """Return the first usable close-approach entry, or None."""
    approaches = record.get("close_approach_data", [])
    if not isinstance(approaches, list):
        return None
    for approach in approaches:
        if isinstance(approach, dict):
            return approach
    return None


def get_max_diameter_km(record: dict[str, Any]) -> float | None:
    """Read the maximum estimated diameter in kilometres."""
    diameter = record.get("estimated_diameter", {})
    if not isinstance(diameter, dict):
        return None
    km = diameter.get("kilometers", {})
    if not isinstance(km, dict):
        return None
    return safe_float(km.get("estimated_diameter_max"))


def get_min_diameter_km(record: dict[str, Any]) -> float | None:
    """Read the minimum estimated diameter in kilometres."""
    diameter = record.get("estimated_diameter", {})
    if not isinstance(diameter, dict):
        return None
    km = diameter.get("kilometers", {})
    if not isinstance(km, dict):
        return None
    return safe_float(km.get("estimated_diameter_min"))


def clean_cohort(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter invalid cohorts, safely parse fields and impute missing magnitude."""
    usable = [
        record for record in records
        if isinstance(record.get("close_approach_data"), list)
        and bool(record["close_approach_data"])
    ]

    magnitude_values = [
        safe_float(record.get("absolute_magnitude_h"))
        for record in usable
    ]
    magnitude_values = [value for value in magnitude_values if value is not None]
    magnitude_median = median(magnitude_values) if magnitude_values else 0.0

    cleaned: list[dict[str, Any]] = []
    for record in usable:
        approach = select_close_approach(record)
        if approach is None:
            continue

        velocity = approach.get("relative_velocity", {})
        distance = approach.get("miss_distance", {})
        if not isinstance(velocity, dict) or not isinstance(distance, dict):
            continue

        max_diameter = get_max_diameter_km(record)
        min_diameter = get_min_diameter_km(record)
        miss_km = safe_float(distance.get("kilometers"))
        miss_ld = safe_float(distance.get("lunar"))
        velocity_kph = safe_float(velocity.get("kilometers_per_hour"))

        if None in (max_diameter, min_diameter, miss_km, miss_ld, velocity_kph):
            continue

        magnitude = safe_float(record.get("absolute_magnitude_h"), magnitude_median)

        cleaned.append(
            {
                "neo_id": str(record.get("neo_reference_id", "")),
                "name": record.get("name", ""),
                "estimated_diameter_max_km": max_diameter,
                "estimated_diameter_min_km": min_diameter,
                "miss_distance_km": miss_km,
                "miss_distance_lunar": miss_ld,
                "relative_velocity_kph": velocity_kph,
                "absolute_magnitude_h": magnitude,
                "num_close_approaches_in_window": len(record["close_approach_data"]),
                "is_potentially_hazardous_asteroid": bool(
                    record.get("is_potentially_hazardous_asteroid", False)
                ),
            }
        )

    return cleaned


def approach_category(miss_distance_lunar: float) -> str:
    """Bucket a miss distance into the required approach category."""
    if miss_distance_lunar <= 5:
        return "very_close"
    if miss_distance_lunar <= 20:
        return "close"
    if miss_distance_lunar <= 60:
        return "moderate"
    return "distant"


def engineer_features(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add ratio, category and independent priority-watch features."""
    for record in records:
        distance = record["miss_distance_lunar"]
        record["size_to_distance_ratio"] = (
            record["estimated_diameter_max_km"] / distance
            if distance != 0
            else float("inf")
        )
        record["approach_category"] = approach_category(distance)
        record["priority_watch"] = int(
            record["estimated_diameter_max_km"] >= SIZE_THRESHOLD_KM
            and distance <= DISTANCE_THRESHOLD_LD
        )
    return records


def load_ground_station_log() -> dict[str, dict[str, str]]:
    """Load the generated ground-station CSV into a lookup dictionary."""
    if not GROUND_LOG_PATH.exists():
        return {}

    with GROUND_LOG_PATH.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return {
            str(row.get("neo_id", "")): row
            for row in reader
            if row.get("neo_id")
        }


def attach_log_fields(
    records: list[dict[str, Any]],
    ground_log: dict[str, dict[str, str]],
    total_known_neos: int | None,
) -> list[dict[str, Any]]:
    """Join optional ground-station fields and the scraped reference count."""
    for record in records:
        match = ground_log.get(record["neo_id"], {})
        record["station_name"] = match.get("station_name")
        record["confidence_score"] = safe_float(match.get("confidence_score"))
        record["tracking_status"] = match.get("tracking_status")
        record["total_known_neos"] = total_known_neos
    return records


def min_max_scale(records: list[dict[str, Any]], field: str) -> None:
    """Apply exact min-max scaling to a numeric field in-place."""
    values = [float(record[field]) for record in records]
    if not values:
        return

    min_x = min(values)
    max_x = max(values)

    for record in records:
        if max_x == min_x:
            record[f"scaled_{field}"] = 0.0
        else:
            record[f"scaled_{field}"] = (
                (float(record[field]) - min_x) / (max_x - min_x)
            )


def scrape_total_known_neos() -> int | None:
    """Scrape the NASA page for the number before the anchor phrase."""
    try:
        response = requests.get(SCRAPE_URL, timeout=30)
        response.raise_for_status()
        html = response.text
    except requests.exceptions.RequestException as exc:
        print(f"Warning: bonus NASA scrape failed: {exc}")
        return None

    anchor = "Total number of discovered near-Earth asteroids"
    index = html.lower().find(anchor.lower())
    if index == -1:
        print("Warning: scrape anchor phrase was not found.")
        return None

    window = html[max(0, index - 250):index]
    numbers = re.findall(r"(?<![\d.])\d[\d,]*(?![\d.])", window)
    if not numbers:
        print("Warning: no number was found before the scrape anchor.")
        return None

    try:
        return int(numbers[-1].replace(",", ""))
    except ValueError:
        return None


def native_stats(records: list[dict[str, Any]], field: str) -> tuple[float, float, float]:
    """Compute min, max and mean in one native-Python pass."""
    if not records:
        raise ValueError("Cannot compute statistics for an empty record set.")

    first = float(records[0][field])
    min_value = first
    max_value = first
    running_sum = 0.0

    for record in records:
        value = float(record[field])
        if value < min_value:
            min_value = value
        if value > max_value:
            max_value = value
        running_sum += value

    return min_value, max_value, running_sum / len(records)


def quality_audit(records: list[dict[str, Any]]) -> dict[str, float]:
    """Calculate missingness and NASA PHA completeness percentages."""
    total = len(records)
    if total == 0:
        return {
            "close_approach_missing_pct": 0.0,
            "absolute_magnitude_missing_pct": 0.0,
            "pha_missing_pct": 0.0,
        }

    close_missing = sum(
        1
        for record in records
        if not record.get("close_approach_data")
    )
    magnitude_missing = sum(
        1
        for record in records
        if record.get("absolute_magnitude_h") in (None, "")
    )
    pha_missing = sum(
        1
        for record in records
        if not isinstance(record.get("is_potentially_hazardous_asteroid"), bool)
    )

    return {
        "close_approach_missing_pct": close_missing / total * 100,
        "absolute_magnitude_missing_pct": magnitude_missing / total * 100,
        "pha_missing_pct": pha_missing / total * 100,
    }


def validation_crosstab(records: list[dict[str, Any]]) -> dict[tuple[bool, bool], int]:
    """Build the required native-Python 2x2 validation table."""
    table = {
        (False, False): 0,
        (False, True): 0,
        (True, False): 0,
        (True, True): 0,
    }

    for record in records:
        sentinel = bool(record["priority_watch"])
        nasa = bool(record["is_potentially_hazardous_asteroid"])
        table[(sentinel, nasa)] += 1

    return table


def write_processed_csv(records: list[dict[str, Any]]) -> None:
    """Write the final cleaned, engineered and joined dataset."""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    if not records:
        raise RuntimeError("No records remain after cleaning.")

    fieldnames = list(records[0].keys())
    with OUTPUT_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


def ensure_ground_log(neo_ids: list[str]) -> None:
    """Create the reproducible generated log if it is absent."""
    if GROUND_LOG_PATH.exists():
        return

    generator = ROOT / "generate_sentinel_log.py"
    if not generator.exists():
        print("Warning: ground-station generator is missing; join fields will be empty.")
        return

    import subprocess
    subprocess.run(
        ["python", str(generator), "--input-ids", str(IDS_PATH)],
        cwd=ROOT,
        check=True,
    )


def main() -> None:
    """Run Project Sentinel from acquisition through validation and packaging."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    api_key = os.getenv("NASA_API_KEY", "DEMO_KEY")
    windows = get_date_windows()
    print(f"Using API windows: {windows}")

    all_records = fetch_neo_records(api_key, windows)
    print(f"Raw API records: {len(all_records)}")

    neo_ids = extract_unique_ids(all_records)
    write_extracted_ids(neo_ids)
    print(f"Extracted {len(neo_ids)} unique NEO IDs.")
    ensure_ground_log(neo_ids)

    total_known_neos = scrape_total_known_neos()
    if total_known_neos is not None:
        print(f"NASA total known NEOs from page: {total_known_neos}")

    audit_before = quality_audit(all_records)
    cleaned = clean_cohort(all_records)
    print(f"Records after close-approach cleaning: {len(cleaned)}")

    for field in (
        "max_diameter_km",
        "miss_distance_km",
        "relative_velocity_kph",
    ):
        actual_field = (
            "estimated_diameter_max_km"
            if field == "max_diameter_km"
            else field
        )
        stats = native_stats(cleaned, actual_field)
        print(
            f"{actual_field}: min={stats[0]:.6f}, "
            f"max={stats[1]:.6f}, mean={stats[2]:.6f}"
        )

    cleaned = engineer_features(cleaned)
    ground_log = load_ground_station_log()
    cleaned = attach_log_fields(cleaned, ground_log, total_known_neos)
    min_max_scale(cleaned, "size_to_distance_ratio")
    write_processed_csv(cleaned)

    n_total = len(cleaned)
    n_flagged = sum(record["priority_watch"] for record in cleaned)
    reduction = (1 - n_flagged / n_total) * 100 if n_total else 0.0

    table = validation_crosstab(cleaned)

    print("\nQuality audit on raw pull:")
    for key, value in audit_before.items():
        print(f"  {key}: {value:.2f}%")

    print("\nValidation crosstab: (Sentinel priority_watch, NASA PHA)")
    for key, value in table.items():
        print(f"  {key}: {value}")

    print(f"\nROI: {reduction:.2f}% workload reduction.")
    if total_known_neos:
        share = n_total / total_known_neos * 100
        print(f"Weekly pull share of all known NEOs: {share:.6f}%")

    print(f"\nWrote: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
