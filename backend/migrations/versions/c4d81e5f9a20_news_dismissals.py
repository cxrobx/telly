"""news_dismissals: per-person Dismiss / Mark watched on What's new."""
from alembic import op
import sqlalchemy as sa

revision = "c4d81e5f9a20"
down_revision = "b7e2a91c4d10"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "news_dismissals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("plex_id", sa.BigInteger(), sa.ForeignKey("users.plex_id"), nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("history_status", sa.String(), nullable=True),
        sa.Column("history_created", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("plex_id", "event_id"),
    )
    op.create_index("ix_news_dismissals_plex_id", "news_dismissals", ["plex_id"])


def downgrade():
    op.drop_index("ix_news_dismissals_plex_id", "news_dismissals")
    op.drop_table("news_dismissals")
