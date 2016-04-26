"""add service.

Revision ID: 23416032b1cf
Revises: 4cf90c101d82
Create Date: 2016-04-26 08:55:57.235411

"""

revision = '23416032b1cf'
down_revision = '4cf90c101d82'

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.create_table('service',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('type', sa.SmallInteger(), nullable=False),
    sa.Column('host', sa.String(length=128), nullable=False),
    sa.Column('port', sa.SmallInteger(), nullable=False),
    sa.Column('secret', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['project.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'host', 'port', name='ux_service_project_host_port')
    )


def downgrade():
    op.drop_table('service')
