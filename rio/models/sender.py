# -*- coding: utf-8 -*-
"""
rio.models.sender
~~~~~~~~~~~~~~~~~
"""

from datetime import datetime

from sqlalchemy import func

from rio.core import db

class Sender(db.Model):
    """to validate trusted senders."""

    __table_name__ = 'rio_sender'
    __table_args__ = (
        db.UniqueConstraint('project_id', 'slug', name='ux_sender_project_and_slug'),
    )

    id = db.Column(db.Integer(), primary_key=True)
    project_id = db.Column(db.Integer(), db.ForeignKey('project.id'), nullable=False)
    slug = db.Column(db.String(64), nullable=False)
    token = db.Column(db.String(40), nullable=False)
    created_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)


def validate(project_id, slug, token):
    """Validate whether sender have authority to emit event to project.

    :param project_id: integer, project id
    :param slug: string, sender slug
    :param token: string, token for sender
    :return: Boolean.
    """
    return bool(
        Sender.query.filter_by(
            project_id=project_id,
            slug=slug,
            token=token
        ).value(func.Count(Sender.id)))
