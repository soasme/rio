# -*- coding: utf-8 -*-
"""
rio.core
~~~~~~~~~

Definition of rio core object.
"""

from flask_sqlalchemy import SQLAlchemy
from flask_celery import Celery
from rio.exts.flask_redis_cluster import RedisCluster

#: db: SQLAlchemy instance for database manipulation.
db = SQLAlchemy()

#: celery: Celery instance for job queue manipulation.
celery = Celery()

#: redis
redis = RedisCluster()
