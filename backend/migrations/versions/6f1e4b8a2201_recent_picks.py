"""Recent Plex watch recommendations and canonical Rotten Tomatoes URLs."""
from alembic import op
import sqlalchemy as sa

revision = "6f1e4b8a2201"
down_revision = "dffa73df0f12"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ratings", sa.Column("rt_url", sa.String(), nullable=True))
    op.create_table("recent_picks",
        sa.Column("plex_id", sa.BigInteger(), sa.ForeignKey("users.plex_id"), primary_key=True),
        sa.Column("picks", sa.JSON(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("recent_picks")
    op.drop_column("ratings", "rt_url")
