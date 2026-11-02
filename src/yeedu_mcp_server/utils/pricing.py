"""Cloud list prices for the instance types Yeedu runs clusters on.

Yeedu has no notion of money. `machine_price_ycu` on the machine-type lookup
looks like a price but is a pure function of the instance's shape -- vCPUs/4 +
memory_GB/10, the formula the UI tooltip gives, which reproduces every row in
that lookup exactly. So a run's YCU tells you how big a machine it held, not
what the cloud charged for holding it. Converting a run into a cost means
multiplying its wall-clock by the provider's rate for that instance type, and
that rate only exists in the provider's price list.

Rates are stored per vCPU-hour per instance family, because on-demand pricing
is linear in size within a family on all three major providers -- m5.2xlarge is
exactly twice m5.xlarge. One rate therefore covers every size in a family,
including sizes this table was never told about. Families that break linearity
(AWS `u-*` and `metal`) are deliberately absent rather than approximated.

Every rate is an on-demand list price for one basis region, captured on a date,
and covers compute only: no storage, no egress, no licensing, no managed
services. An organisation on committed-use, reserved or EDP pricing pays
materially less. The table is therefore overridable -- point
YEEDU_MCP_PRICE_TABLE at a JSON file to replace or extend it with the rates
that org actually pays:

    {
      "as_of": "2026-10-30",
      "currency": "USD",
      "providers": {
        "AWS":    {"m6i": {"usd_per_vcpu_hour": 0.031, "basis_region": "us-east-1",
                           "source": "our EDP rate card"}},
        "ONPREM": {"*":   {"usd_per_vcpu_hour": 0.012, "basis_region": "dc1",
                           "source": "internal chargeback"}}
      }
    }

A family of "*" matches any instance type for that provider, which is how
on-prem gets a rate at all: there is no public price for a machine someone
already owns, so only the operator can say what an hour of it costs.
"""

import json
import logging
import os
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Env var naming a JSON file that replaces or extends the seeded rates.
PRICE_TABLE_ENV = "YEEDU_MCP_PRICE_TABLE"

#: When the seeded rates below were captured. Stamped onto every estimate so a
#: stale table is visible in the answer rather than silently believed.
SEED_AS_OF = "2026-10-30"

SEED_CURRENCY = "USD"

#: provider -> family -> rate record. Derived by dividing a verified on-demand
#: hourly rate for one size by that size's vCPU count; the division is recorded
#: in `source` so each number can be re-checked against the provider.
SEED_RATES: Dict[str, Dict[str, Dict[str, Any]]] = {
    "AWS": {
        "m5": {"usd_per_vcpu_hour": 0.0480, "basis_region": "us-east-1",
               "source": "m5.xlarge $0.192/hr / 4 vCPU"},
        "m6i": {"usd_per_vcpu_hour": 0.0480, "basis_region": "us-east-1",
                "source": "m6i.xlarge $0.192/hr / 4 vCPU"},
        "r6i": {"usd_per_vcpu_hour": 0.0630, "basis_region": "us-east-1",
                "source": "r6i.xlarge $0.252/hr / 4 vCPU"},
        "c6i": {"usd_per_vcpu_hour": 0.0425, "basis_region": "us-east-1",
                "source": "c6i.xlarge $0.170/hr / 4 vCPU"},
    },
    "GCP": {
        "n2-standard": {"usd_per_vcpu_hour": 0.04855, "basis_region": "us-central1",
                        "source": "n2-standard-4 $0.1942/hr / 4 vCPU"},
        "n2d-standard": {"usd_per_vcpu_hour": 0.04225, "basis_region": "us-central1",
                         "source": "n2d-standard-4 $0.1690/hr / 4 vCPU"},
        "e2-standard": {"usd_per_vcpu_hour": 0.03350, "basis_region": "us-central1",
                        "source": "e2-standard-4 $0.1340/hr / 4 vCPU"},
    },
    "AZURE": {
        "Ds_v5": {"usd_per_vcpu_hour": 0.0480, "basis_region": "eastus",
                  "source": "Standard_D4s_v5 $0.192/hr / 4 vCPU"},
        # Captured in West US 3 -- the only region a rate was found for -- so
        # its basis differs from the Ds_v5 row above. Carried per family rather
        # than per provider precisely so this stays visible.
        "Es_v5": {"usd_per_vcpu_hour": 0.0630, "basis_region": "westus3",
                  "source": "Standard_E4s_v5 $0.252/hr / 4 vCPU"},
    },
}

#: Cached merge of seed and override. None until first use.
_table: Optional[Dict[str, Any]] = None


class PriceUnavailable(Exception):
    """No rate is known for an instance type, with the reason in the message."""


def _load_override() -> Optional[Dict[str, Any]]:
    path = os.environ.get(PRICE_TABLE_ENV)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        # A broken override must not silently fall back to list prices that the
        # org does not pay -- but it must not break every cost call either, so
        # warn and carry on with the seed, which the answer stamps as such.
        logger.warning("Ignoring %s: %s", PRICE_TABLE_ENV, exc)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("providers"), dict):
        logger.warning("Ignoring %s: no 'providers' object", PRICE_TABLE_ENV)
        return None
    return data


def price_table() -> Dict[str, Any]:
    """Return the merged rate table, loading and caching the override once."""
    global _table
    if _table is not None:
        return _table

    table: Dict[str, Any] = {
        "as_of": SEED_AS_OF,
        "currency": SEED_CURRENCY,
        "overridden": False,
        "providers": {k: dict(v) for k, v in SEED_RATES.items()},
    }

    override = _load_override()
    if override:
        table["as_of"] = override.get("as_of") or table["as_of"]
        table["currency"] = override.get("currency") or table["currency"]
        table["overridden"] = True
        for provider, families in override["providers"].items():
            if not isinstance(families, dict):
                continue
            merged = dict(table["providers"].get(provider.upper(), {}))
            for family, record in families.items():
                if isinstance(record, dict) and "usd_per_vcpu_hour" in record:
                    merged[family] = record
            table["providers"][provider.upper()] = merged

    _table = table
    return table


def reset_cache() -> None:
    """Drop the cached table. For tests that set the override env var."""
    global _table
    _table = None


def family_of(provider: str, instance_type: str) -> Optional[str]:
    """Reduce an instance type to the family its rate is keyed by.

    Each provider names sizes differently, so each needs its own reduction:
    AWS puts the size after a dot (`m6i.4xlarge` -> `m6i`), GCP after the last
    hyphen (`n2-highmem-8` -> `n2-highmem`, which must stay distinct from
    `n2-standard` because the two do not cost the same), and Azure embeds the
    vCPU count in the middle of the name (`Standard_D4as_v5` -> `Das_v5`).
    """
    name = (instance_type or "").strip()
    if not name:
        return None

    key = (provider or "").upper()

    if key == "AWS":
        return name.split(".")[0] or None

    if key == "GCP":
        # Drop a trailing size group, keeping the series and the shape.
        trimmed = re.sub(r"-\d+$", "", name)
        return trimmed or None

    if key == "AZURE":
        size = re.sub(r"^Standard_", "", name, flags=re.I)
        # Azure's own family names are the size with the vCPU count removed:
        # D4as_v5 belongs to the Das_v5 family. The version suffix has to be
        # split off first, or stripping digits takes the 5 out of v5 too and
        # every generation collapses into one family.
        match = re.match(r"^(.*?)(_?v\d+)$", size, flags=re.I)
        head, version = (match.group(1), match.group(2)) if match else (size, "")
        return (re.sub(r"\d+", "", head) + version).strip("_") or None

    return name or None


def rate_for(provider: str, instance_type: str) -> Dict[str, Any]:
    """Return the rate record for an instance type.

    Raises PriceUnavailable, with a message that says what to do about it,
    rather than guessing a rate for an unknown family.
    """
    table = price_table()
    key = (provider or "").upper()
    families = table["providers"].get(key)

    if not families:
        if key in ("ONPREM", "", None):
            raise PriceUnavailable(
                "this cluster runs on-prem, and there is no public hourly price "
                "for hardware the operator already owns. Set "
                f"{PRICE_TABLE_ENV} to a JSON file giving "
                '{"providers": {"ONPREM": {"*": {"usd_per_vcpu_hour": <your '
                'internal chargeback rate>}}}} to cost on-prem runs.'
            )
        raise PriceUnavailable(
            f"no rates are seeded for provider {provider}. Add them via "
            f"{PRICE_TABLE_ENV}."
        )

    family = family_of(key, instance_type)
    record = families.get(family) if family else None
    if record is None and "*" in families:
        # Report a wildcard match as "*" rather than as the family it stood in
        # for, so the answer shows the rate was a provider-wide catch-all and
        # not one priced for this instance type.
        record, family = families["*"], "*"
    if record is None:
        raise PriceUnavailable(
            f"no rate is seeded for {provider} family "
            f"{family or instance_type!r} (instance type {instance_type}). "
            f"Linear families are covered at any size; this one is either "
            f"absent or priced non-linearly, so add it via {PRICE_TABLE_ENV} "
            f"rather than inferring it from a neighbouring family."
        )

    return {
        "usd_per_vcpu_hour": float(record["usd_per_vcpu_hour"]),
        "family": family,
        "basis_region": record.get("basis_region"),
        "source": record.get("source"),
        "currency": table["currency"],
        "as_of": table["as_of"],
        "overridden": table["overridden"],
    }
