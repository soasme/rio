"""init

Revision ID: 4cf90c101d82
Revises: None
Create Date: 2016-04-12 14:31:03.123085

"""

# revision identifiers, used by Alembic.
revision = '4cf90c101d82'
down_revision = None

from alembic import op
import sqlalchemy as sa


def upgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.create_table('user',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('confirmed_at', sa.DateTime(), nullable=True),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('nickname', sa.String(length=32), server_default='', nullable=False),
    sa.Column('mobile', sa.String(length=11), nullable=True),
    sa.Column('email', sa.String(length=128), nullable=False),
    sa.Column('password', sa.String(length=128), server_default='', nullable=False),
    sa.Column('reset_password_token', sa.String(length=128), server_default='', nullable=False),
    sa.Column('auth_token', sa.String(length=128), nullable=True),
    sa.Column('active', sa.Boolean(), server_default='0', nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('auth_token', name='ux_user_auth_token'),
    sa.UniqueConstraint('email', name='ux_user_email'),
    sa.UniqueConstraint('mobile', name='ux_user_mobile'),
    sa.UniqueConstraint('username', name='ux_user_username')
    )
    op.create_table('project',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('owner_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.SmallInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['owner_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug', name='ux_project_slug')
    )
    op.create_table('sender',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('token', sa.String(length=40), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['project.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'slug', name='ux_sender_project_and_slug')
    )
    op.create_table('action',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['project.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'slug', name='ux_action_project_slug')
    )
    op.create_table('webhook',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('method_id', sa.SmallInteger(), nullable=False),
    sa.Column('action_id', sa.Integer(), nullable=False),
    sa.Column('url', sa.String(length=1024), nullable=False),
    sa.Column('json_headers', sa.String(length=2048)),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['action_id'], ['action.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('action_id', 'url', name='ux_webhook_subscribe')
    )
    ### end Alembic commands ###


def downgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.drop_table('webhook')
    op.drop_table('action')
    op.drop_table('sender')
    op.drop_table('project')
    op.drop_table('user')
    ### end Alembic commands ###
