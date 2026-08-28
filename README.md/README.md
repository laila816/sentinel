# Project Sentinel — Automated Near-Earth Object Triage

## 1. Project Overview

**Domain:** Aerospace / Planetary Defense

Project Sentinel builds an automated pre-triage layer for newly tracked near-Earth objects (NEOs). It combines NASA NeoWs close-approach data with an internal ground-station log, engineers a small set of decision-support features, and flags objects that combine meaningful estimated size with a notably close pass.

### Objective

The independent triage rule is:

```text
priority_watch = 1
if (max_estimated_diameter_km >= 0.14) AND (miss_distance_lunar <= 10)
else 0
```

The 0.14 km threshold is the stated size benchmark in the project brief, while 10 lunar distances (LD) is the stated close-pass cutoff. This classifier is intentionally different from NASA's `is_potentially_hazardous_asteroid` definition.

## 2. Resource Audit

| Resource | Use |
|---|---|
| NASA NeoWs Feed API | Live NEO close-approach acquisition |
| `data/raw/ground_station_log.csv` | Generated internal reconciliation log |
| NASA Planetary Defense page | Bonus web-scrape enrichment |
| `requests` | HTTP/API calls |
| `json` | JSON parsing |
| `csv` | CSV input/output |
| `pathlib` | Cross-platform file paths |

**Explicitly not used:** `pandas`, `numpy`.

### API note

NASA's `DEMO_KEY` is rate-limited and shared by IP. A personal NASA API key is recommended for repeat runs.

## 3. Data Acquisition

The pipeline requests two adjacent 7-day windows because the NeoWs feed has a hard maximum of 7 days per request. The two result sets are merged and NEO IDs are de-duplicated while preserving first-seen order.

The API response is nested:

```text
near_earth_objects
    -> date
        -> list of objects
            -> close_approach_data
```

The pipeline explicitly handles:
- dictionary-keyed dates;
- quoted numeric values in `relative_velocity` and `miss_distance`;
- empty `close_approach_data`;
- request failures;
- missing `absolute_magnitude_h`.

## 4. Features

The project uses more than the required six brainstormed features:

1. `estimated_diameter_max_km`
2. `estimated_diameter_min_km`
3. `miss_distance_km`
4. `miss_distance_lunar`
5. `relative_velocity_kph`
6. `absolute_magnitude_h`
7. `num_close_approaches_in_window`
8. `confidence_score`
9. `size_to_distance_ratio`
10. `approach_category`
11. `priority_watch`
12. `is_potentially_hazardous_asteroid`

## 5. Data Preparation

### Cohort filtering

Records with an empty `close_approach_data` list are excluded because no close-approach distance can be computed.

### Type safety

All conversions of API values that may arrive as strings are performed through a reusable `safe_float()` helper with `try/except`.

### Imputation

If `absolute_magnitude_h` is missing, the cohort median is used. This is preferable to dropping the record because the field is needed for analysis but its absence does not mean the NEO itself is unusable.

### Feature engineering

```text
size_to_distance_ratio = max_diameter_km / miss_distance_lunar
```

Approach categories:

- `very_close`: <= 5 LD
- `close`: <= 20 LD
- `moderate`: <= 60 LD
- `distant`: > 60 LD

### Min-Max scaling

```text
scaled_x = (x - min_x) / (max_x - min_x)
```

The denominator is checked for the zero-range case so the pipeline remains safe if every ratio is identical.

## 6. Ground-Station Join

The generated ground-station log is loaded into a dictionary keyed by `neo_id`.

The join uses `.get()` rather than direct indexing, so:
- API records without a ground-station match are retained;
- ghost log rows that do not exist in the API cohort are harmless.

The generated log intentionally contains approximately 10% dropped API IDs and approximately 10% ghost IDs, as required by the brief.

## 7. Bonus Web Scrape

The pipeline fetches the NASA Planetary Defense page and extracts the number immediately before:

```text
Total number of discovered near-Earth asteroids
```

That value is stored as `total_known_neos` on every processed row.

The README metric is then:

```text
weekly_pull_share_pct = (n_total / total_known_neos) * 100
```

## 8. ROI Headline Metric

The project computes:

```text
pct_workload_reduction = (1 - (n_flagged / n_total)) * 100
```

The exact value is produced from the current cleaned dataset by the pipeline and printed in the terminal. Because NASA's live feed changes over time, the metric is intentionally generated at run time rather than hard-coded.

The README headline to report after the run is:

> **An analyst who only manually reviews `priority_watch == 1` objects cuts their weekly review set by X%.**

Replace **X** with the value printed by the pipeline after the live run.

## 9. Validation

The pipeline creates a 2×2 cross-tab between:

- Project Sentinel `priority_watch`
- NASA `is_potentially_hazardous_asteroid`

The four combinations are:

| Sentinel | NASA PHA | Meaning |
|---:|---:|---|
| 0 | 0 | Both do not flag |
| 0 | 1 | NASA flags, Sentinel does not |
| 1 | 0 | Sentinel flags, NASA does not |
| 1 | 1 | Both flag |

Disagreement is expected. The two rules measure different concepts: Sentinel uses estimated size plus close-approach distance, while NASA's PHA flag is based on orbital geometry / minimum orbit intersection distance and absolute magnitude rather than this project's direct size-and-distance rule.

## 10. Repository Structure

```text
project_repo/
├── data/
│   ├── raw/
│   │   ├── extracted_ids.txt
│   │   └── ground_station_log.csv
│   └── processed/
│       └── clean_data.csv
├── notebooks/
│   └── exploration.ipynb
├── src/
│   └── pipeline.py
├── generate_sentinel_log.py
└── README.md
```

`generate_sentinel_log.py` is included as a small reproducible helper because the brief references a log generator but does not provide its source code as part of the supplied project materials.

## 11. How to Run

### 1. Install the only external dependency

```bash
pip install requests
```

### 2. Set a NASA API key

Windows PowerShell:

```powershell
$env:NASA_API_KEY="YOUR_KEY"
```

macOS/Linux:

```bash
export NASA_API_KEY="YOUR_KEY"
```

If no key is supplied, the script falls back to `DEMO_KEY`.

### 3. Run the full pipeline

From the repository root:

```bash
python src/pipeline.py
```

The script will:
1. request two adjacent API windows;
2. extract and de-duplicate NEO IDs;
3. create the ground-station log if it is missing;
4. scrape the NASA reference count;
5. clean and engineer the cohort;
6. join the ground-station fields;
7. scale the ratio feature;
8. create the validation cross-tab;
9. write `data/processed/clean_data.csv`.

### 4. Open the notebook

Open:

```text
notebooks/exploration.ipynb
```

and run all cells after the pipeline has produced the raw/processed data.

## 12. Submission Checklist

- [x] README with objectives and resource audit
- [x] Explicit target formula
- [x] 6+ brainstormed features
- [x] ROI formula
- [x] Native Python EDA
- [x] Recursive structural audit
- [x] Missingness/completeness audit
- [x] Safe numeric conversion
- [x] Cohort filtering
- [x] Median imputation
- [x] Feature engineering
- [x] Ground-station dictionary join
- [x] Min-Max scaling
- [x] 2×2 validation table
- [x] PEP-8-oriented code
- [x] Function docstrings
- [x] Standalone `__main__` entry point
- [x] No pandas
- [x] No numpy

**Important:** `clean_data.csv` is a live-data output. Run `python src/pipeline.py` immediately before submission so the CSV, notebook outputs, and README metric correspond to the same API pull.
