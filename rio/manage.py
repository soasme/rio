# -*- coding: utf-8 -*-

from flask_script import Manager
from flask_migrate import MigrateCommand
from rio.app import create_app as create_flask
from rio.core import celery
from rio.core import migrate

app = create_flask()

manager = Manager(app)
manager.add_command('db', MigrateCommand)

def main():
    manager.run()
