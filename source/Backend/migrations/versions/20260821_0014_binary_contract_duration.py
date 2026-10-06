"""Add account-scoped Binary contract duration.

Revision ID: 20260821_0014
Revises: 20260821_0013
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260821_0014"
down_revision: Union[str, None] = "20260821_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("deriv_binary_accounts")}
    if "binary_duration_minutes" not in columns:
        op.add_column(
            "deriv_binary_accounts",
            sa.Column("binary_duration_minutes", sa.Integer(), nullable=False, server_default="15"),
        )
    constraints = {item["name"] for item in sa.inspect(op.get_bind()).get_check_constraints("deriv_binary_accounts")}
    if "ck_deriv_binary_duration_minutes" not in constraints:
        op.create_check_constraint(
            "ck_deriv_binary_duration_minutes",
            "deriv_binary_accounts",
            "binary_duration_minutes BETWEEN 1 AND 60",
        )


def downgrade() -> None:
    constraints = {item["name"] for item in sa.inspect(op.get_bind()).get_check_constraints("deriv_binary_accounts")}
    if "ck_deriv_binary_duration_minutes" in constraints:
        op.drop_constraint("ck_deriv_binary_duration_minutes", "deriv_binary_accounts", type_="check")
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("deriv_binary_accounts")}
    if "binary_duration_minutes" in columns:
        op.drop_column("deriv_binary_accounts", "binary_duration_minutes")
