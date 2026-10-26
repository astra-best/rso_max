"""Generate bounded synthetic 1C JSON for importer checks."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

MAX_ACCOUNTS = 1000
DUPLICATE_METER_ID = "00000003-0000-4000-8000-999999999999"


def synthetic_guid(kind: int, index: int) -> str:
    """Deterministic GUID: importer accepts only GUID IDs; kind separates entities."""
    return f"{kind:08d}-0000-4000-8000-{index:012d}"


def build_payload(count: int, *, duplicate_meter_id: bool = False) -> list[dict]:
    if not isinstance(count, int) or count < 0 or count > MAX_ACCOUNTS:
        raise ValueError(f"count must be between 0 and {MAX_ACCOUNTS}")
    records = []
    for index in range(count):
        meter_id = (
            DUPLICATE_METER_ID if duplicate_meter_id and index < 2
            else synthetic_guid(3, index)
        )
        records.append({
            "PersonalAccountID": synthetic_guid(1, index),
            "RoomKeyID": synthetic_guid(0, index),
            "PersonalAccount": f"SYNTH-{index:04d}",
            "MeteringPoints": [{
                "MeteringPointID": synthetic_guid(2, index),
                "MeteringPoint": "Synthetic point",
                "ResourceID": "RESOURCE-1",
                "Resource": "Electricity",
                "Meters": [{
                    "MeterID": meter_id,
                    "MeterNumber": f"SERIAL-{index:04d}",
                    "DifferentiationTypeID": "TYPE-1T",
                    "DifferentiationType": "Single tariff",
                    "Scales": [{
                        "ScaleID": synthetic_guid(4, index),
                        "Scale": "Main",
                        "TariffZones": [{
                            "TariffZoneID": synthetic_guid(5, index),
                            "TariffZone": "T1",
                        }],
                    }],
                }],
            }],
        })
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--duplicate-meter-id", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("synthetic-1c.txt"))
    args = parser.parse_args()
    payload = build_payload(args.count, duplicate_meter_id=args.duplicate_meter_id)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Generated {len(payload)} accounts: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
