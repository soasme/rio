.. rio documentation master file, created by
   sphinx-quickstart on Thu Apr  7 09:59:46 2016.
   You can adapt this file completely to your liking, but it should at least
   contain the root `toctree` directive.

Welcome to rio's documentation!
===============================

Rio is a simple, scalable and reliable distributed event-driven
system.

Rio receives actions via RESTful APIs and then triggers a bunch
of subscribed webhooks simultaneously and asynchronously.

Rio is still working in progress. Rio is a thin wrapper of Celery
and Flask, allowing you to use multiple kinds of broker to run
async webhooks, such as RabbitMQ, Redis, ZeroMQ, SQLAlchemy, etc.
It is Open Source and licensed under BSD License.

Contents:

.. toctree::
   :maxdepth: 2

   quick_start
   introduction
   installation
   upgrading
   configuration
   broker
   storage_backend
   worker
   cli
   monitoring
   api_reference


Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`

