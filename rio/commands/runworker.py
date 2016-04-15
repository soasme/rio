# -*- coding: utf-8 -*-
"""
rio.commands.runworker
~~~~~~~~~~~~~~~~~~~~~~
"""

from flask_script import Command
from celery.bin.worker import worker

from rio.core import celery

class RunWorkerCommand(Command):
    """Run celery worker"""

    def run(self):
        worker(app=celery).run(app=celery)
