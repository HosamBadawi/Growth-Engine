"""baseline v2 schema

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-24

Adopts the current declarative Base as the Alembic baseline. Uses create_all
(checkfirst) so it is a no-op on a database that already has the tables — an
existing v1/v2 install upgrades in place, its data untouched — while a fresh
database gets the full schema. Future schema changes ship as new revisions.
"""
from typing import Sequence, Union

from alembic import op

from db.models import Base

revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
