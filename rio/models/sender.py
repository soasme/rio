# -*- coding: utf-8 -*-
"""
rio.models.sender
~~~~~~~~~~~~~~~~~
"""

from datetime import datetime

from sqlalchemy import func

from rio.core import db
from .utils import ins2dict

class Sender(db.Model):
    """to validate trusted senders."""

    __table_name__ = 'sender'
    __table_args__ = (
        db.UniqueConstraint('project_id', 'slug', name='ux_sender_project_and_slug'),
    )

    id = db.Column(db.Integer(), primary_key=True)
    project_id = db.Column(db.Integer(), db.ForeignKey('project.id'), nullable=False)
    slug = db.Column(db.String(64), nullable=False)
    token = db.Column(db.String(40), nullable=False)
    created_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)

    def to_sensitive_dict(self):
        return dict(
            id=self.id,
            project_id=self.project_id,
            slug=self.slug,
            token=self.token,
        )

    def to_full_dict(self):
        data = ins2dict(self)
        data.pop('project_id')
        data['project'] = ins2dict(getattr(self, 'project'), 'simple')
        return data
