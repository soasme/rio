# -*- coding: utf-8 -*-

from datetime import datetime

from rio.core import db

class Webhook(db.Model):
    """Webhook Model.

    Webhook model defines the http method and url that will be triggered
    on receiving event.
    """

    __tablename__ = 'webhook'
    __table_args__ = (
        db.UniqueConstraint('topic_id', 'url', name='ux_webhook_subscribe'),
        db.ForeignKeyConstraint(
            ['topic_id'], ['topic.id'], ondelete='CASCADE', name='fk_webhook_topic'
        ),
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
    topic_id = db.Column(db.Integer(), nullable=False)
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
