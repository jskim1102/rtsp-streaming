"""add version to ip_cams

Revision ID: 7d6e38756974
Revises: 7927ae6eadfb
Create Date: 2026-11-24 14:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7d6e38756974'
down_revision: Union[str, Sequence[str], None] = '7927ae6eadfb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """낙관적 락용 버전 번호 — 화면이 연 뒤 다른 사람이 먼저 수정했는지 비교한다.

    - 기존 행은 1로 시작한다 (server_default).
    """
    op.add_column("ip_cams", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))


def downgrade() -> None:
    """Downgrade schema."""
    # SQLite 는 컬럼 삭제 지원이 버전마다 달라 batch(테이블 재생성)로 지운다.
    with op.batch_alter_table("ip_cams") as batch:
        batch.drop_column("version")
