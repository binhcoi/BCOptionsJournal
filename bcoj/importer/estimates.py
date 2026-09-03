"""Reconstructed share lots, loaded from configuration.

A legacy sheet that only has rows for option trades cannot record an outright
share purchase, so a ticker's share count can end up short: more shares
disposed than the record accounts for. Matching then has nothing to draw on and
correctly refuses to guess.

Where the missing purchase is remembered, it is described here rather than
invented by the importer. These entries name real holdings, so they live in a
config file outside version control -- never in source.

Location, in order of preference:

  1. ``$BCOJ_ESTIMATES``
  2. ``private/estimates.json`` beside the repository root

Format::

    {
      "estimates": [
        {
          "underlying": "XYZ",
          "quantity": 100,
          "cost_per_share": "35",
          "note": "why this figure is believed, and how firmly"
        }
      ]
    }

Absent the file, no estimates are seeded and tickers in deficit stay blocked --
which is the safe default: a blocked ticker is visible, a guessed one is not.
"""

import json
import os
import pathlib
from dataclasses import dataclass
from decimal import Decimal

ENV_VAR = "BCOJ_ESTIMATES"
DEFAULT_PATH = pathlib.Path(__file__).resolve().parents[2] / "private" / "estimates.json"


@dataclass(frozen=True)
class EstimatedLot:
    """A share acquisition reconstructed from memory rather than from a record.

    Always flagged in the journal, so a figure resting on it is never mistaken
    for one resting on a broker record.
    """

    underlying: str
    quantity: int
    cost_per_share: Decimal
    note: str = ""


def config_path() -> pathlib.Path:
    override = os.environ.get(ENV_VAR)
    return pathlib.Path(override) if override else DEFAULT_PATH


def load(path=None) -> tuple[EstimatedLot, ...]:
    """Read configured estimates. Returns empty if the file is absent."""
    target = pathlib.Path(path) if path else config_path()
    if not target.exists():
        return ()

    with open(target, encoding="utf-8") as handle:
        payload = json.load(handle)

    out = []
    for entry in payload.get("estimates", []):
        out.append(
            EstimatedLot(
                underlying=str(entry["underlying"]).upper(),
                quantity=int(entry["quantity"]),
                # str() first: a JSON number would arrive as a float.
                cost_per_share=Decimal(str(entry["cost_per_share"])),
                note=entry.get("note", ""),
            )
        )
    return tuple(out)
