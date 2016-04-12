# -*- coding: utf-8 -*-
"""
rio.exts.flask_redis_cluster
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""
from flask import current_app

from rb import Cluster

from .base import Extension



class RedisCluster(Extension):
    """Redis cluster extension."""

    def init_extension(self, app):
        app.config.setdefault('REDIS_DEFAULT_CLUSTERS', {
            0: {'port': 6379, 'host': '127.0.0.1'},
        })

    @property
    def default(self):
        return self.context('redis_defalut_cluster', self.make_redis_default_cluster)

    def make_redis_default_cluster(self):
        return Cluster(
            hosts=current_app.config.get('REDIS_DEFAULT_CLUSTERS'),
        )
