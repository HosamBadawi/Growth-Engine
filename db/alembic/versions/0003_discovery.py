"""v2.2: contact-discovery fields, find runs, rejected candidates.

Additive only. Existing prospects keep every value they had; new columns are
nullable or defaulted and backfilled from what is already known.

Revision ID: 0003_discovery
Revises: 0002_dedupe_key
"""
import sqlalchemy as sa
from alembic import op

revision = "0003_discovery"
down_revision = "0002_dedupe_key"
branch_labels = None
depends_on = None


NEW_COLUMNS = (
    ("social_links", sa.JSON(), None),
    ("provenance", sa.JSON(), None),
    ("country", sa.String(length=2), None),
    ("discovery_partial", sa.Boolean(), sa.text("0")),
    ("attempt_count", sa.Integer(), sa.text("0")),
)


def _existing(table: str) -> set[str]:
    bind = op.get_bind()
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    have = _existing("prospects")
    for name, type_, default in NEW_COLUMNS:
        if name in have:
            continue  # tolerate a DB already carrying the column (create_all path)
        op.add_column("prospects",
                      sa.Column(name, type_, nullable=True, server_default=default))

    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "find_runs" not in tables:
        op.create_table(
            "find_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("started_at", sa.DateTime(), nullable=True, index=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("query", sa.String(length=500), nullable=True),
            sa.Column("provider", sa.String(length=50), nullable=True),
            sa.Column("target", sa.Integer(), nullable=True),
            sa.Column("summary_json", sa.JSON(), nullable=True),
        )
    if "rejected_candidates" not in tables:
        op.create_table(
            "rejected_candidates",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("run_id", sa.Integer(),
                      sa.ForeignKey("find_runs.id"), nullable=True, index=True),
            sa.Column("ts", sa.DateTime(), nullable=True, index=True),
            sa.Column("name", sa.String(length=255), nullable=True),
            sa.Column("city", sa.String(length=100), nullable=True),
            sa.Column("country", sa.String(length=2), nullable=True),
            sa.Column("reason", sa.String(length=200), nullable=True, index=True),
            sa.Column("raw_json", sa.JSON(), nullable=True),
            sa.Column("retried", sa.Boolean(), nullable=True, server_default=sa.text("0")),
        )

    # Backfill: empty JSON containers and sane defaults for existing rows.
    op.execute("UPDATE prospects SET social_links = '{}' WHERE social_links IS NULL")
    op.execute("UPDATE prospects SET provenance = '{}' WHERE provenance IS NULL")
    op.execute("UPDATE prospects SET discovery_partial = 0 WHERE discovery_partial IS NULL")
    op.execute("UPDATE prospects SET attempt_count = 0 WHERE attempt_count IS NULL")
    # Country from the US state registries we already ran; everything else stays
    # NULL rather than guessing wrong.
    op.execute("UPDATE prospects SET country = 'US' "
               "WHERE country IS NULL AND source LIKE 'registry%'")


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "rejected_candidates" in tables:
        op.drop_table("rejected_candidates")
    if "find_runs" in tables:
        op.drop_table("find_runs")
    have = _existing("prospects")
    for name, _type, _default in reversed(NEW_COLUMNS):
        if name in have:
            op.drop_column("prospects", name)
