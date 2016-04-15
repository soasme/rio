# -*- coding: utf-8 -*-
"""
rio.core
~~~~~~~~~

Definition of rio core object.
"""

import logging

from flask_sqlalchemy import SQLAlchemy
from flask_celery import Celery
from flask_migrate import Migrate
from flask_user import UserManager

from raven.contrib.flask import Sentry
from rio.exts.flask_redis_cluster import RedisCluster
from rio.exts.flask_cache import Cache

#: db: SQLAlchemy instance for database manipulation.
db = SQLAlchemy()

#: celery: Celery instance for job queue manipulation.
celery = Celery()

#: redis
redis = RedisCluster()

#: redis cache
cache = Cache()

#: logger instance
logger = logging.getLogger('rio')

#: sentry: Sentry Official Client
sentry = Sentry()

#: migrate: Database migration object
migrate = Migrate()

#: User manager
user_manager = UserManager()
