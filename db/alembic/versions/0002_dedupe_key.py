"""add prospects.dedupe_key (indexed, backfilled)

Revision ID: 0002_dedupe_key
Revises: 0001_baseline
Create Date: 2026-07-25

Additive only: adds a nullable column plus an index, then backfills every
existing row from data already present (intel_json.license_no, else name|city).
No row is deleted or rewritten beyond that single column, so an operator's live
database upgrades in place.
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_dedupe_key"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _key(license_no, name, city) -> str:
    if license_no and str(license_no).strip():
        return str(license_no).strip().lower()
    return f"{(name or '').strip()}|{(city or '').strip()}".lower()


def upgrade() -> None:
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns("prospects")}
    if "dedupe_key" not in existing:
        op.add_column("prospects", sa.Column("dedupe_key", sa.String(320), nullable=True))
        op.create_index("ix_prospects_dedupe_key", "prospects", ["dedupe_key"])

    rows = bind.execute(
        sa.text("SELECT id, name, city, intel_json FROM prospects "
                "WHERE dedupe_key IS NULL OR dedupe_key = ''")
    ).fetchall()
    for row in rows:
        intel = row[3]
        if isinstance(intel, (str, bytes)):
            try:
                intel = json.loads(intel)
            except (ValueError, TypeError):
                intel = {}
        license_no = (intel or {}).get("license_no") if isinstance(intel, dict) else None
        bind.execute(
            sa.text("UPDATE prospects SET dedupe_key = :key WHERE id = :id"),
            {"key": _key(license_no, row[1], row[2]), "id": row[0]},
        )


def downgrade() -> None:
    op.drop_index("ix_prospects_dedupe_key", table_name="prospects")
    op.drop_column("prospects", "dedupe_key")
