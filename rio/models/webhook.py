# -*- coding: utf-8 -*-

from datetime import datetime

from rio.core import db
from rio._compat import json
from .utils import ins2dict

class Webhook(db.Model):
    """Webhook Model.

    Webhook model defines the http method and url that will be triggered
    on receiving event.
    """

    __tablename__ = 'webhook'
    __table_args__ = (
        db.UniqueConstraint('action_id', 'url', name='ux_webhook_subscribe'),
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
            POST: 'POST',
        }

    id = db.Column(db.Integer(), primary_key=True)
    method_id = db.Column(db.SmallInteger(), nullable=False, default=Method.GET)
    action_id = db.Column(db.Integer(), db.ForeignKey('action.id'), nullable=False)
    url = db.Column(db.String(1024), nullable=False)
    json_headers = db.Column(db.String(2048))
    created_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)

    @property
    def method(self):
        return self.Method.MAP[self.method_id]

    @method.setter
    def method(self, method):
        self.method_id = getattr(self.Method, method.upper())

    @property
    def headers(self):
        if not self.json_headers:
            return {}
        return json.loads(self.json_headers)

    @headers.setter
    def headers(self, headers):
        self.json_headers = json.dumps(headers)

    def to_full_dict(self):
        data = ins2dict(self)
        data.pop('action_id')
        data['action'] = ins2dict(getattr(self, 'action'), 'simple')
        return data

    def to_simple_dict(self):
        return dict(
            id=self.id,
            method=self.method,
            url=self.url,
            headers=self.headers or {},
        )
