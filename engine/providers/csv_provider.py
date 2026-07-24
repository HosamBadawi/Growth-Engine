"""CSV import provider: manual lists or exports from any other tool.

Expected headers (extra columns ignored, all optional except name):
name,category,phone,website,address,city,state,rating,review_count,maps_url,email
Usage: /find csv:path/to/file.csv anything 20  (or set PROSPECT_PROVIDER=csv and
pass the file path as the query).
"""
import csv
import logging
from pathlib import Path

from engine.providers.base import ProspectProvider, RawProspect, parse_city_state

log = logging.getLogger("prospector.csv")


class CsvProvider(ProspectProvider):
    name = "csv"

    def search(self, query: str, limit: int) -> list[RawProspect]:
        path = Path(query.removeprefix("csv:").strip())
        if not path.exists():
            raise FileNotFoundError(f"CSV file not found: {path}")
        prospects = []
        with path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                name = row.get("name") or row.get("title")
                if not name:
                    continue
                city, state = row.get("city", ""), row.get("state", "")
                if not city or not state:
                    parsed_city, parsed_state = parse_city_state(row.get("address", ""))
                    city, state = city or parsed_city, state or parsed_state
                try:
                    rating = float(row["rating"]) if row.get("rating") else None
                except ValueError:
                    rating = None
                try:
                    review_count = int(row["review_count"]) if row.get("review_count") else None
                except ValueError:
                    review_count = None
                prospects.append(
                    RawProspect(
                        name=name,
                        category=row.get("category", ""),
                        phone=row.get("phone", ""),
                        website=row.get("website", ""),
                        address=row.get("address", ""),
                        city=city,
                        state=state,
                        rating=rating,
                        review_count=review_count,
                        maps_url=row.get("maps_url", ""),
                        emails=[row["email"]] if row.get("email") else [],
                        source=self.name,
                    )
                )
        return prospects[:limit]
