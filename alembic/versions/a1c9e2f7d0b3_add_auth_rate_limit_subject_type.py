"""add auth rate limit subject type

Revision ID: a1c9e2f7d0b3
Revises: f48daaaa1f42
Create Date: 2026-07-31 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c9e2f7d0b3'
down_revision: Union[str, Sequence[str], None] = 'f48daaaa1f42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Extend the subject_type CHECK constraint to allow 'auth', used by the
    # new rate limiting on /login and /register (TICKET-102). SQLite has no
    # ALTER CONSTRAINT support, so batch mode recreates the table.
    with op.batch_alter_table('rate_limit_events', schema=None) as batch_op:
        batch_op.drop_constraint('ck_rate_limit_subject_type', type_='check')
        batch_op.create_check_constraint(
            'ck_rate_limit_subject_type',
            "subject_type IN ('user','session','resolve_user','resolve_session','auth')",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('rate_limit_events', schema=None) as batch_op:
        batch_op.drop_constraint('ck_rate_limit_subject_type', type_='check')
        batch_op.create_check_constraint(
            'ck_rate_limit_subject_type',
            "subject_type IN ('user','session','resolve_user','resolve_session')",
        )
