"""Migrate legacy replication

Revision ID: a87f7ecc4e88
Revises: ed69a9a6fab1
Create Date: 2019-10-26 20:24:27.220287+00:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a87f7ecc4e88'
down_revision = '6d65fd64e91c'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.execute("UPDATE storage_replication SET repl_transport = 'SSH' WHERE repl_transport = 'LEGACY'")
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    pass
    # ### end Alembic commands ###