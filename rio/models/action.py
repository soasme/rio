# -*- coding: utf-8 -*-

from datetime import datetime

from rio.core import db
from .utils import ins2dict

class Action(db.Model):
    """Action Model.

    Actions are emitted and subscribed as identity of event.
    """

    __tablename__ = 'action'
    __table_args__ = (
        db.UniqueConstraint('project_id', 'slug', name='ux_action_project_slug'),
    )

    id = db.Column(db.Integer(), primary_key=True)
    slug = db.Column(db.String(64), nullable=False)
    project_id = db.Column(db.Integer(), db.ForeignKey('project.id'), nullable=False)
    description = db.Column(db.String(255), nullable=False, default='')
    created_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)

    webhooks = db.relationship('Webhook', backref=db.backref('action'), lazy='dynamic')

    def to_full_dict(self):
        data = ins2dict(self)
        project_id = data.pop('project_id')
        data['project'] = ins2dict(self.project, 'simple')
        data['webhooks'] = [ins2dict(webhook, 'simple') for webhook in self.webhooks]
        return data

    def to_simple_dict(self):
        return dict(
            slug=self.slug,
        )
