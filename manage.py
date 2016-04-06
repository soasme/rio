# -*- coding: utf-8 -*-

from flask_script import Manager
from rio.app import create_app as create_flask
from rio.core import celery


app = create_flask()
manager = Manager(app)


if __name__ == '__main__':
    manager.run()
