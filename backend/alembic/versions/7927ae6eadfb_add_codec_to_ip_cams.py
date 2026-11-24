"""add codec to ip_cams

Revision ID: 7927ae6eadfb
Revises: eb81928ad755
Create Date: 2026-11-24 13:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7927ae6eadfb'
down_revision: Union[str, Sequence[str], None] = 'eb81928ad755'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """코덱 판별 결과 저장 — 재등록 때 ffprobe 를 다시 돌리지 않는다.

    - 기존 행은 NULL(모름)로 남는다 — 동기화가 재등록할 때 판별해 채운다.
    """
    op.add_column("ip_cams", sa.Column("codec", sa.String(length=20), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    # SQLite 는 컬럼 삭제 지원이 버전마다 달라 batch(테이블 재생성)로 지운다.
    with op.batch_alter_table("ip_cams") as batch:
        batch.drop_column("codec")
