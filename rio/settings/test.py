# -*- coding: utf-8 -*-

DEBUG = True
TESTING = True

SECRET_KEY = 'no-secret'

SQLALCHEMY_TRACK_MODIFICATIONS = False
SQLALCHEMY_DATABASE_URI = 'sqlite:////tmp/rio-test.db'

CELERY_BROKER_URL = 'sqla+' + SQLALCHEMY_DATABASE_URI
CELERY_RESULT_BACKEND = 'db+' + SQLALCHEMY_DATABASE_URI

REDIS_DEFAULT_CLUSTERS = [
    {
        'host': 'localhost',
        'port': 6379,
    }
]
