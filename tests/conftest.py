# -*- coding: utf-8 -*-

import json
import logging
from uuid import uuid4
from os import environ

from pytest import fixture

from rio.app import create_app
from rio.core import db as db

@fixture(scope='session')
def app(request):
    environ['RIO_ENV'] = 'test'
    app_ = create_app()
    ctx = app_.app_context()
    ctx.push()
    request.addfinalizer(ctx.pop)
    return app_


@fixture(scope='session')
def database(request, app):
    uuid = uuid4().hex
    for table in db.metadata.sorted_tables:
        table.name = 'test_%s_%s' % (uuid, table.name)
    db.create_all()
    def fin():
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
    request.addfinalizer(fin)
    return db

@fixture
def session(request, database):
    # cache.mc._flushall()
    def fin():
        for table in reversed(database.metadata.sorted_tables):
            database.session.execute(table.delete())
        database.session.commit()
        database.session.close()
        # cache.mc._flushall()
    request.addfinalizer(fin)
    return database.session

class Client(object):
    """
    Utility class for tests
    """
    def __init__(self, client):
        self.client = client

    def post(self, *args, **kwargs):
        resp = self.client.post(*args, **kwargs)
        if resp.headers.get('Content-Type') == 'application/json':
            resp.json = json.loads(resp.data)
        else:
            resp.json = None
        return resp

    def __getattr__(self, key):
        return getattr(self.client, key)


@fixture
def client(app, request):
    ctx = app.test_request_context()
    ctx.push()
    request.addfinalizer(ctx.pop)
    return Client(app.test_client())


def pytest_addoption(parser):
    '''
    Add CLI options to py.test
    '''
    group = parser.getgroup('logging', 'Logging Configuration')
    group.addoption('--logging-level',
                    dest='logging_level',
                    default='INFO',
                    help='log format as used by the logging module')
    parser.addini('logging_level',
                  'log level as used by the logging module')

def pytest_configure(config):
    logging.root.addHandler(logging.StreamHandler())
    logging.root.setLevel(
        getattr(logging, config.getini('logging_level') or
                config.getvalue('logging_level')))
