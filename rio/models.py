# -*- coding: utf-8 -*-
"""
rio.models
~~~~~~~~~~~

Definitions of rio models based on SQLAlchemy.
"""

from datetime import datetime

from .core import db

class Topic(db.Model):
    """Topic Model.

    Topics are published and subscribed as identity of event.
    """

    __tablename__ = 'topic'
    __table_args__ = (
        db.UniqueConstraint('title', name='ux_topic_title'),
    )

    id = db.Column(db.Integer(), primary_key=True)
    title = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)
    webhooks = db.relationship('Webhook', backref='topic', lazy='dynamic')

class Webhook(db.Model):
    """Webhook Model.

    Webhook model defines the http method and url that will be triggered
    on receiving event.
    """

    __tablename__ = 'webhook'
    __table_args__ = (
        db.UniqueConstraint('topic_id', 'url', name='ux_webhook_subscribe'),
        db.Index('ix_webhook_topic', 'topic_id'),
    )

    class Method:
        """HTTP Methods.

        Currently, only GET/POST are supported.
        For database effencity, this field will be stored as integer.
        """
        GET = 1
        POST = 2
        MAP = {
            GET: 'GET',
            GET: 'POST',
        }

    id = db.Column(db.Integer(), primary_key=True)
    method_id = db.Column(db.SmallInteger(), nullable=False, default=Method.GET)
    topic_id = db.Column(db.Integer(), db.ForeignKey('topic.id'), nullable=False)
    url = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)

    @property
    def method(self):
        return self.Method.MAP[self.method_id]

    def to_dict(self):
        return dict(
            id=self.id,
            method=self.method,
            url=self.url,
            topic=self.topic.title,
        )
