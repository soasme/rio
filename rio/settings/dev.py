# -*- coding: utf-8 -*-

DEBUG = True

SECRET_KEY = 'OOxdXBtiwPHGpjxaACWvzpYCbDhBmaYk'

SQLALCHEMY_DATABASE_URI = 'sqlite:////tmp/rio.db'

CELERY_BROKER_URL = 'sqla+' + SQLALCHEMY_DATABASE_URI
CELERY_RESULT_BACKEND = 'db+' + SQLALCHEMY_DATABASE_URI

REDIS_DEFAULT_CLUSTERS = {
    0: {'host': '127.0.0.1', 'port': 6379},
}

GRAPH_BACKEND = 'sqlalchemy'
