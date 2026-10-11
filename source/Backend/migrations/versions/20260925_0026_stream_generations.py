"""Add immutable stream generation namespaces without rewriting legacy records."""

from alembic import op
import sqlalchemy as sa

revision = "20260925_0026"
down_revision = "20260917_0025"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "indicator_stream_generations",
        sa.Column("root_key", sa.String(20), primary_key=True),
        sa.Column("timeframe", sa.String(10), primary_key=True),
        sa.Column("generation", sa.Integer(), primary_key=True),
        sa.Column("storage_key", sa.String(20), nullable=False),
        sa.Column("scope", sa.String(100)),
        sa.Column("public_symbol", sa.String(20), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("configuration_version", sa.String(50), nullable=False),
        sa.Column("activation_watermark", sa.DateTime(timezone=True)),
        sa.Column("history_hash", sa.String(64)),
        sa.Column("predecessor_snapshot", sa.JSON()),
        sa.Column("bootstrap_state", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("storage_key", "timeframe", name="uq_generation_storage"),
    )
    op.create_index(
        "uq_generation_active",
        "indicator_stream_generations",
        ["root_key", "timeframe"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
        sqlite_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_table(
        "indicator_stream_heads",
        sa.Column("root_key", sa.String(20), primary_key=True),
        sa.Column("timeframe", sa.String(10), primary_key=True),
        sa.Column("active_generation", sa.Integer(), nullable=False),
    )
    op.create_table(
        "strategy_setup_generations",
        sa.Column(
            "setup_id",
            sa.String(96),
            sa.ForeignKey("strategy_setup_lifecycle.setup_id"),
            primary_key=True,
        ),
        sa.Column("root_key", sa.String(20), primary_key=True),
        sa.Column("timeframe", sa.String(10), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmation_time", sa.DateTime(timezone=True), nullable=False),
    )
    bind = op.get_bind()
    # Additive assignment only: legacy IDs, row values and foreign keys remain byte-for-byte intact.
    states = sa.table(
        "indicator_stream_state",
        sa.column("symbol"),
        sa.column("timeframe"),
        sa.column("configuration_version"),
        sa.column("activation_watermark"),
        sa.column("updated_at"),
    )
    registry = sa.table(
        "indicator_stream_generations",
        *[
            sa.column(n)
            for n in [
                "root_key",
                "timeframe",
                "generation",
                "storage_key",
                "public_symbol",
                "status",
                "configuration_version",
                "activation_watermark",
                "created_at",
            ]
        ]
    )
    heads = sa.table(
        "indicator_stream_heads",
        sa.column("root_key"),
        sa.column("timeframe"),
        sa.column("active_generation"),
    )
    for r in bind.execute(sa.select(states)).mappings():
        bind.execute(
            registry.insert().values(
                root_key=r["symbol"],
                timeframe=r["timeframe"],
                generation=1,
                storage_key=r["symbol"],
                public_symbol=r["symbol"].split("~")[0],
                status="ACTIVE",
                configuration_version=r["configuration_version"],
                activation_watermark=r["activation_watermark"],
                created_at=r["updated_at"],
            )
        )
        bind.execute(
            heads.insert().values(
                root_key=r["symbol"], timeframe=r["timeframe"], active_generation=1
            )
        )


def downgrade():
    bind = op.get_bind()
    if (
        bind.execute(
            sa.text(
                "SELECT count(*) FROM indicator_stream_generations WHERE generation > 1"
            )
        ).scalar()
        or bind.execute(
            sa.text("SELECT count(*) FROM strategy_setup_generations")
        ).scalar()
    ):
        raise RuntimeError("Cannot downgrade durable generation history")
    op.drop_table("strategy_setup_generations")
    op.drop_table("indicator_stream_heads")
    op.drop_index("uq_generation_active", table_name="indicator_stream_generations")
    op.drop_table("indicator_stream_generations")
