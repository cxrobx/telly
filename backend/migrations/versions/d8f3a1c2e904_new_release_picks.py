"""new_release_picks: personalized fresh releases for What's new."""
from alembic import op
import sqlalchemy as sa

revision = "d8f3a1c2e904"
down_revision = "c4d81e5f9a20"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "new_release_picks",
        sa.Column("plex_id", sa.BigInteger(), sa.ForeignKey("users.plex_id"), primary_key=True),
        sa.Column("picks", sa.JSON(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("new_release_picks")
