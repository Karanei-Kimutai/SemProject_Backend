"""drop invitations (team invitations were removed)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-23 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(op.f('ix_invitations_workspace_id'), table_name='invitations')
    op.drop_index('ix_invitations_email', table_name='invitations')
    op.drop_table('invitations')


def downgrade() -> None:
    op.create_table('invitations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('role', sa.Enum('OWNER', 'MANAGER', 'VIEWER', name='role', native_enum=False, length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('invited_by', sa.Uuid(), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('responded_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['invited_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_invitations_email', 'invitations', ['email', 'status'], unique=False)
    op.create_index(op.f('ix_invitations_workspace_id'), 'invitations', ['workspace_id'], unique=False)
