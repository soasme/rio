# -*- coding: utf-8 -*-

from flask import current_app

from rediscluster import StrictRedisCluster

from .base import Extension

class RedisCluster(Extension):

    def init_extension(self, app):
        assert app.config.get('REDIS_DEFAULT_CLUSTERS')
        app.config.setdefault('REDIS_BUFFER_CLUSTERS', app.config.get('REDIS_DEFAULT_CLUSTERS'))
        app.config.setdefault('REDIS_QUOTA_CLUSTERS', app.config.get('REDIS_DEFAULT_CLUSTERS'))
        app.config.setdefault('REDIS_TSDB_CLUSTERS', app.config.get('REDIS_DEFAULT_CLUSTERS'))

    @property
    def default(self):
        return self.context('redis_defalut_cluster', self.make_redis_default_cluster)

    def make_redis_default_cluster(self):
        return StrictRedisCluster(
            startup_nodes=current_app.config.get('REDIS_DEFAULT_CLUSTERS')
        )

    @property
    def buffer(self):
        return self.context('redis_buffer_cluster', self.make_redis_buffer_cluster)

    def make_redis_buffer_cluster(self):
        if current_app.config.get('REDIS_BUFFER_CLUSTERS') == \
           current_app.config.get('REDIS_DEFALUT_CLUSTERS'):
            return self.default
        return StrictRedisCluster(
            startup_nodes=current_app.config.get('REDIS_BUFFER_CLUSTERS')
        )
