# -*- coding: utf-8 -*-
"""
rio.exts.flask_redis_cache
~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

from time import time

from flask import current_app
from werkzeug.utils import import_string

from rio._compat import json
from .base import Extension

class MemoryClient(object):

    TABLE = {}
    EXPIRES = {}

    def get(self, key):
        if key not in self.EXPIRES or self.EXPIRES[key] > time():
            return self.TABLE.get(key)
        else:
            del self.TABLE[key]
            del self.EXPIRES[key]
            return

    def set(self, key, value):
        self.TABLE[key] = value
        if key in self.EXPIRES:
            del self.EXPIRES[key]

    def delete(self, key):
        if key in self.TABLE:
            del self.TABLE[key]
        if key in self.EXPIRES:
            del self.TABLE[key]

    def setex(self, key, timeout, value):
        self.TABLE[key] = value
        self.EXPIRES[key] = time() + timeout


class RedisClient(object):

    def __init__(self, **kwargs):
        assert hasattr(current_app, 'extensions')
        cluster = current_app.extensions.get('rediscluster')
        assert cluster, 'Should init cluster before init cache.'
        self._client = cluster.default.get_routing_client()

    def get(self, key):
        return self._client.get(key)

    def set(self, key, value):
        return self._client.set(key, value)

    def delete(self, key):
        return self._client.delete(key)

    def setex(self, key, timeout, value):
        return self._client.setex(key, timeout, value)


class Cache(Extension):

    def init_extension(self, app):
        """Initialize cache instance."""
        app.config.setdefault('CACHE_VERSION', '0')
        app.config.setdefault('CACHE_PREFIX', 'r')
        app.config.setdefault('CACHE_BACKEND', 'rio.exts.flask_cache.MemoryClient')
        app.config.setdefault('CACHE_BACKEND_OPTIONS', {})

    @property
    def version(self):
        return current_app.config.get('REDIS_CACHE_VERSION')

    @property
    def prefix(self):
        return current_app.config.get('REDIS_CACHE_PREFIX')

    def make_key(self, key, version=None):
        """RedisCache will set prefix+version as prefix for each key."""
        return '{}:{}:{}'.format(
            self.prefix,
            version or self.version,
            key,
        )

    @property
    def client(self):
        """This method should return a client that implements:

        * set
        * get
        * setex
        * delete
        """
        return self.context('cacheclient', self.get_client)

    def get_client(self):
        """Get cache client.
        """
        backend_class = import_string(current_app.config.get('CACHE_BACKEND'))
        backend = backend_class(**current_app.config.get('CACHE_BACKEND_OPTIONS'))
        return backend


    def run(self, fn, ns='', version=None, **kwargs):
        kwargs_key = ':'.join('%s:%s' % (
            k, str(kwargs[k]).replace(' ', '')) for k in sorted(kwargs.keys()))
        key = '%s:%s:%s' % (ns, fn.__name__, kwargs_key)
        key = self.make_key(key, version)

        data = self.get(key, version)
        if data is not None:
            return data
        else:
            data = fn(**kwargs)
            if data is not None:
                self.set(key, data, 86400, version)
            return data

    def set(self, key, value, timeout=None, version=None):
        """Execute `SET key value timeout` in redis.
        """
        key = self.make_key(key, version=version)
        v = json.dumps(value)
        if timeout:
            self.client.setex(key, int(timeout), v)
        else:
            self.client.set(key, v)

    def delete(self, key, version=None):
        """Execute `DEL key` in redis."""
        key = self.make_key(key, version=version)
        self.client.delete(key)

    def get(self, key, version=None):
        """Execute `GET key` in redis."""
        key = self.make_key(key, version=version)
        result = self.client.get(key)
        if result is not None:
            result = json.loads(result)
        return result
