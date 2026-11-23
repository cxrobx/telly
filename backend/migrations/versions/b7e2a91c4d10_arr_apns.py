"""Radarr/Sonarr: events.media_type (movie in_plex events), arr_notices, apns_devices."""
from alembic import op
import sqlalchemy as sa

revision = "b7e2a91c4d10"
down_revision = "72ac5b1093ed"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("events") as b:
        b.add_column(sa.Column("media_type", sa.String(), nullable=False, server_default="tv"))
    op.create_table(
        "apns_devices",
        sa.Column("token", sa.String(length=200), primary_key=True),
        sa.Column("plex_id", sa.BigInteger(), sa.ForeignKey("users.plex_id"), nullable=False),
        sa.Column("environment", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_apns_devices_plex_id", "apns_devices", ["plex_id"])
    op.create_table(
        "arr_notices",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("app", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("arr_id", sa.Integer(), nullable=True),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("detail", sa.String(), nullable=False),
        sa.Column("dedupe_key", sa.String(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pushed", sa.Integer(), nullable=False),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index("ix_arr_notices_received_at", "arr_notices", ["received_at"])


def downgrade():
    op.drop_index("ix_arr_notices_received_at", "arr_notices")
    op.drop_table("arr_notices")
    op.drop_index("ix_apns_devices_plex_id", "apns_devices")
    op.drop_table("apns_devices")
    with op.batch_alter_table("events") as b:
        b.drop_column("media_type")
