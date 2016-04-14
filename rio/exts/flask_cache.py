# -*- coding: utf-8 -*-
"""
rio.exts.flask_redis_cache
~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""
import logging
from time import time

from flask import current_app
from werkzeug.utils import import_string

from rio._compat import json
from .base import Extension

logger = logging.getLogger('rio.exts.flask_cache')

class MemoryBackend(object):

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
            del self.EXPIRES[key]

    def setex(self, key, timeout, value):
        self.TABLE[key] = value
        self.EXPIRES[key] = time() + timeout


class RedisBackend(object):

    def __init__(self, **kwargs):
        assert hasattr(current_app, 'extensions')
        cluster = current_app.extensions.get('rediscluster')
        assert cluster, 'Should init cluster before init cache.'
        self._client = cluster.default.get_routing_client()

    def get(self, key):
        """Execute `GET key` in redis."""
        return self._client.get(key)

    def set(self, key, value):
        """Execute `SET key value timeout` in redis. """
        return self._client.set(key, value)

    def delete(self, key):
        """Execute `DEL key` in redis."""
        return self._client.delete(key)

    def setex(self, key, timeout, value):
        return self._client.setex(key, timeout, value)


class Cache(Extension):

    def init_extension(self, app):
        """Initialize cache instance."""
        app.config.setdefault('CACHE_VERSION', '0')
        app.config.setdefault('CACHE_PREFIX', 'r')
        app.config.setdefault('CACHE_BACKEND', 'rio.exts.flask_cache.MemoryBackend')
        app.config.setdefault('CACHE_BACKEND_OPTIONS', {})

    @property
    def version(self):
        return current_app.config.get('CACHE_VERSION')

    @property
    def prefix(self):
        return current_app.config.get('CACHE_PREFIX')

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

        data = self.get(key, version)
        if data is not None:
            logger.debug('HIT %s', self.make_key(key))
            return data
        else:
            logger.debug('MISS %s', self.make_key(key))
            data = fn(**kwargs)
            if data is not None:
                logger.debug('CACHE %s', self.make_key(key))
                self.set(key, data, 86400, version)
            else:
                logger.debug('NIL %s', self.make_key(key))
            return data

    def clean(self, fn, ns='', version=None, **kwargs):
        kwargs_key = ':'.join('%s:%s' % (
            k, str(kwargs[k]).replace(' ', '')) for k in sorted(kwargs.keys()))
        key = '%s:%s:%s' % (ns, fn.__name__, kwargs_key)
        self.delete(key, version)

    def set(self, key, value, timeout=None, version=None):
        key = self.make_key(key, version=version)
        v = json.dumps(value)
        if timeout:
            self.client.setex(key, int(timeout), v)
        else:
            self.client.set(key, v)

    def delete(self, key, version=None):
        key = self.make_key(key, version=version)
        self.client.delete(key)

    def get(self, key, version=None):
        key = self.make_key(key, version=version)
        result = self.client.get(key)
        if result is not None:
            result = json.loads(result)
        return result
