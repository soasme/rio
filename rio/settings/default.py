# -*- coding: utf-8 -*-

RIO_VERSION = '0.2.3'

CELERY_IMPORTS = [
    'celery.task.http',
]

CELERY_CHORD_PROPAGATES = True

SQLALCHEMY_TRACK_MODIFICATIONS = True
