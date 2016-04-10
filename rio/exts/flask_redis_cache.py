# -*- coding: utf-8 -*-
"""
rio.exts.flask_redis_cache
~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""
from flask import current_app

from rio._compat import json
from .base import Extension

class RedisCache(Extension):

    def init_extension(self, app):
        app.config.setdefault('REDIS_CACHE_VERSION', '0')
        app.config.setdefault('REDIS_CACHE_PREFIX', 'r')

    @property
    def version(self):
        return current_app.config.get('REDIS_CACHE_VERSION')

    @property
    def prefix(self):
        return current_app.config.get('REDIS_CACHE_PREFIX')

    def make_key(self, key, version=None):
        return '{}:{}:{}'.format(
            self.prefix,
            version or self.version,
            key,
        )

    @property
    def client(self):
        assert hasattr(current_app, 'extensions')
        cluster = current_app.extensions.get('rediscluster')
        assert cluster, 'Should init cluster before init cache.'
        return cluster.default.get_routing_client()

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
