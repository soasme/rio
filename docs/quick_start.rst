Quick Start
==============

Rio is designed to be out-of-the-box, yet powerful to extend. If you
never have experience in using Rio before, this tutorial will help
you getting started.

Getting started with Rio is a three steps process:

* :ref:`installing_the_rio_server`
* :ref:`configure_events_and_their_calling_webhooks`
* :ref:`configure_the_client`

.. _installing_the_rio_server:

Installing the Rio server
--------------------------

For more details about how to install the Rio server, see :ref:`installation`.

Basically, you need a Unix based OS, Python 2.7. You can use a database as
broker and backend, or use redis as broker and backend, or mix using RabbitMQ
as broker and database as backend. It's all up to you.

.. _configure_events_and_their_calling_webhooks:

Configure an Integration
------------------------

To send messages from your project to Rio you will need to use an SDK which
support your platform. If you can not find platform listed below, you can
simply use the JSON APIs to send messages.

Below is a list of Rio SDKs:

* Python SDK

.. _configure_the_client:

Configure The DSN
-----------------

After you have created a project in Rio, you will be given a DSN value.
This is basically similar to Sentry DSN. It is a standard URL and a
configuration parameter for Rio clients. The DSN can be found in Rio by
navigation to project settings.
