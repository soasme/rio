# -*- coding: utf-8 -*-

from datetime import datetime

from rio.core import db

class Topic(db.Model):
    """Topic Model.

    Topics are published and subscribed as identity of event.
    """

    __tablename__ = 'topic'
    __table_args__ = (
        db.UniqueConstraint('title', name='ux_topic_title'),
        db.ForeignKeyConstraint(
            ['project_id'], ['project.id'], ondelete='CASCADE', name='fk_topic_project'
        ),
    )

    id = db.Column(db.Integer(), primary_key=True)
    title = db.Column(db.String(64), nullable=False)
    project_id = db.Column(db.Integer(), nullable=False)
    created_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)
    webhooks = db.relationship('Webhook', backref='topic', lazy='dynamic')
