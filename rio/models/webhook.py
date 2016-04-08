# -*- coding: utf-8 -*-

from datetime import datetime

from rio.core import db
from .utils import ins2dict

class Webhook(db.Model):
    """Webhook Model.

    Webhook model defines the http method and url that will be triggered
    on receiving event.
    """

    __tablename__ = 'rio_webhook'
    __table_args__ = (
        db.UniqueConstraint('topic_id', 'url', name='ux_webhook_subscribe'),
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
    topic_id = db.Column(db.Integer(), db.ForeignKey('rio_topic.id'), nullable=False)
    url = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)

    @property
    def method(self):
        return self.Method.MAP[self.method_id]

    def to_full_dict(self):
        data = ins2dict(self)
        data.pop('topic_id')
        data['topic'] = ins2dict(self.topic, 'simple')
        return data

    def to_simple_dict(self):
        return dict(
            method=self.method,
            url=self.url,
        )
