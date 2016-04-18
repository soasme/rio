# -*- coding: utf-8 -*-

from flask_script import Manager
from flask_migrate import MigrateCommand
from rio.app import create_app as create_flask
from rio.core import celery
from rio.core import migrate
from rio.commands import RunWorkerCommand
from rio.commands import SyncProjectCommand

app = create_flask()

manager = Manager(app)
manager.add_command('db', MigrateCommand)
manager.add_command('runworker', RunWorkerCommand())
manager.add_command('syncproject', SyncProjectCommand())

def main():
    manager.run()
