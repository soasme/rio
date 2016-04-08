# -*- coding: utf-8 -*-

DEBUG = True

SECRET_KEY = 'OOxdXBtiwPHGpjxaACWvzpYCbDhBmaYk'

SQLALCHEMY_DATABASE_URI = 'sqlite:////tmp/rio.db'

CELERY_BROKER_URL = 'redis://localhost/2'
CELERY_RESULT_BACKEND = 'db+' + SQLALCHEMY_DATABASE_URI
