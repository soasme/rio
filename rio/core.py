# -*- coding: utf-8 -*-
"""
rio.core
~~~~~~~~~

Definition of rio core object.
"""

import logging

from flask_sqlalchemy import SQLAlchemy
from flask_celery import Celery
from raven.contrib.flask import Sentry
from rio.exts.flask_redis_cluster import RedisCluster
from rio.exts.flask_redis_cache import RedisCache

#: db: SQLAlchemy instance for database manipulation.
db = SQLAlchemy()

#: celery: Celery instance for job queue manipulation.
celery = Celery()

#: redis
redis = RedisCluster()

#: redis cache
cache = RedisCache()

#: logger instance
logger = logging.getLogger('rio')

#: sentry: Sentry Official Client
sentry = Sentry()
