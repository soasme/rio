# -*- coding: utf-8 -*-

from datetime import datetime
import hashlib

from sqlalchemy.types import BINARY

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
        db.UniqueConstraint('action_id', 'url_digest', name='ux_webhook_subscribe'),
    )

    class Method:
        """HTTP Methods.

        Currently, only GET/POST/PUT/DELETE are supported.
        For database effencity, this field will be stored as integer.
        """
        GET = 1
        POST = 2
        PUT = 3
        DELETE = 4
        MAP = {
            GET: 'GET',
            POST: 'POST',
            PUT: 'PUT',
            DELETE: 'DELETE',
        }

    id = db.Column(db.Integer(), primary_key=True)
    method_id = db.Column(db.SmallInteger(), nullable=False, default=Method.GET)
    action_id = db.Column(db.Integer(), db.ForeignKey('action.id'), nullable=False)
    url_digest = db.Column(db.String(32), nullable=False)
    raw_url = db.Column(db.String(1024), nullable=False)
    json_headers = db.Column(db.String(2048))
    created_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)

    def __init__(self, action_id, url, method='GET', headers=None):
        self.action_id = action_id
        self.url = url
        self.method = method
        self.headers = headers or {}

    @classmethod
    def query_filter_by(cls, **kwargs):
        queries = dict(kwargs)
        if 'method' in queries and 'url' in queries:
            queries['url_digest'] = cls.generate_url_hash(
                queries.pop('method'), queries.pop('url')
            )
        elif 'method_id' in queries and 'url' in queries:
            method = cls.Method.MAP[queries.pop('method_id')]
            queries['url_digest'] = cls.generate_url_hash(method, queries.pop('url'))
        return cls.query.filter_by(**queries)

    @staticmethod
    def generate_url_hash(method, url):
        content = method.lower() + '.' + url.lower()
        return hashlib.md5(content).hexdigest()

    @property
    def url(self):
        return self.raw_url

    @url.setter
    def url(self, value):
        self.raw_url = value
        if self.method_id:
            method = self.Method.MAP[self.method_id]
            self.url_digest = self.generate_url_hash(method, value)

    @property
    def method(self):
        return self.Method.MAP[self.method_id]

    @method.setter
    def method(self, method):
        self.method_id = getattr(self.Method, method.upper())
        if self.raw_url:
            self.url_digest = self.generate_url_hash(method, self.raw_url)

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
