# -*- coding: utf-8 -*-

from datetime import datetime

from .core import db

class Topic(db.Model):

    __tablename__ = 'topic'

    id = db.Column(db.Integer(), primary_key=True)
    title = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)
    webhooks = db.relationship('Webhook', backref='topic', lazy='dynamic')

class Webhook(db.Model):

    __tablename__ = 'webhook'
    __table_args__ = (
        db.UniqueConstraint('topic_id', 'url', name='ux_webhook_subscribe'),
        db.Index('ix_webhook_topic', 'topic_id'),
    )

    class Method:
        HEAD = 0
        GET = 1
        POST = 2
        PUT = 3
        DELETE = 4
        PATCH = 5
        MAP = {
            0: 'HEAD',
            1: 'GET',
            2: 'POST',
            3: 'PUT',
            4: 'DELETE',
            5: 'PATCH',
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
