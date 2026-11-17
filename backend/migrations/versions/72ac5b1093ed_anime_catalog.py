"""Durable public anime catalog."""
from alembic import op
import sqlalchemy as sa
revision = "72ac5b1093ed"
down_revision = "6f1e4b8a2201"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("anime_catalog", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("picks", sa.JSON(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("anime_catalog")
