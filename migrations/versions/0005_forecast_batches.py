"""forecast batches

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-09 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('forecast_batches',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('store_id', sa.Uuid(), nullable=False),
    sa.Column('requested_by', sa.Uuid(), nullable=True),
    sa.Column('method', sa.Enum('AI', 'BASELINE', 'COMPARE', name='forecastmethod', native_enum=False, length=20), nullable=False),
    sa.Column('as_of_date', sa.Date(), nullable=False),
    sa.Column('horizon_days', sa.Integer(), nullable=False),
    sa.Column('history_version', sa.Integer(), nullable=False),
    sa.Column('product_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['requested_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['store_id'], ['stores.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_forecast_batches_store', 'forecast_batches', ['store_id', 'created_at'], unique=False)
    op.add_column('forecast_runs', sa.Column('batch_id', sa.Uuid(), nullable=True))
    op.create_index(op.f('ix_forecast_runs_batch_id'), 'forecast_runs', ['batch_id'], unique=False)
    op.create_foreign_key('fk_forecast_runs_batch_id', 'forecast_runs', 'forecast_batches', ['batch_id'], ['id'],
                          ondelete='CASCADE')


def downgrade() -> None:
    op.drop_constraint('fk_forecast_runs_batch_id', 'forecast_runs', type_='foreignkey')
    op.drop_index(op.f('ix_forecast_runs_batch_id'), table_name='forecast_runs')
    op.drop_column('forecast_runs', 'batch_id')
    op.drop_index('ix_forecast_batches_store', table_name='forecast_batches')
    op.drop_table('forecast_batches')
