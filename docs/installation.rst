.. _`installation`:

Installation
=============

This guide will step you through setting up a Python-based virtualenv, installing
the required packages, and configuring the basic web service.

Dependencies
------------

Some basic prerequisites which you’ll need in order to run Rio:

* A UNIX-based operating system.
* Python 2.7
* python-setuptools, python-pip, python-dev, libffi-dev, libssl-dev, libyaml-dev
* A broker. It might be one of RabbitMQ(recommend), Redis(recommend), MongoDB,
  ZeroMQ, CouchDB, SQLAlchemy, Django ORM, Amazon SQS, and more..
* A result store. It might be one of AMQP, Redis, memcached, MongoDB, SQLAlchemy,
  Django ORM, Cassandra
* Nginx (nginx-full)
* A dedicated domain to host Rio on (i.e. rio.your-corp.com).

If you’re building from source you’ll also need:

Node.js 4 or newer.

Setting up an Environment
--------------------------

The first thing you’ll need is the Python `virtualenv` package.
You probably already have this, but if not, you can install it with::

    $ pip install -U virtualenv

It’s also available as python-virtualenv on ubuntu in the package manager.

Once that’s done, choose a location for the environment, and create it with the
virtualenv command. For our guide, we're going to choose `/var/www/rio`::

    $ mkdir /var/www/rio
    $ virtualenv --distribute /var/www/rio

Finally, activate your virtualenv::

    $ source /www/rio/bin/activate

Install Rio
------------

Once you’ve got the environment setup, you can install Rio and all its dependencies
with the same command you used to grab virtualenv::

    $ pip install -U rio


To check installation successfully, run Rio CLI, via `rio`::

    $ rio --help

Installing from Source
-----------------------

If you are going to install from source, you will need to install `npm`.
Once your system is prepared, symlink your source into the virtualenv::

    $ python setup.py develop

Initializing the Configuration
------------------------------

To create default configuration, you will use the `init` subcommand of `rio`.
You can specify an alternative configuration path as the argument to init,
otherwise it will use the default of current directory::

    $ rio init /etc/rio

Set `RIO_CONF` as an environment variable so that rio can find this directory
later::

    $ export RIO_CONF=/etc/rio

The `init` subcommand create a config.py. Use your flavoured text editor
to edit `config.py` file to adjust to your infrastructure.

You need to configure:

* :ref:`configure_broker`
* :ref:`configure_storage_backend`


Running Migrations
-------------------

Rio provides an easy way to run migrations on the database on version upgrades.
Before running it for the first time you’ll need to make sure you’ve created the
database::

    mysql> CREATE DATABASE rio;

Once done, you can create the initial schema using the upgrade command::

    $ rio upgrade

Starting the Web Service
------------------------

Rio provides a built-in webserver (powered by Gunicorn) to get you off the ground
quickly. You can also setup Rio as WSGI application by specifying wsgi application
`rio.app:app`. To start the built-in webserver run `rio start`::

    $ rio start web

You should now be able to test the web service by visiting http://localhost:8009/.

Starting Background Workers
---------------------------

A large amount of Rio’s work is managed via background workers. These need run in
addition to the web service workers::

    $ rio start worker


Process Management
------------------

It is recommended to using process management software to keep Rio processes alive.
`supervisor` is a fancy tool to archive that. This is an example of supervisor
config part::

    [program:rio-web]
    directory=/www/rio/
    environment=RIO_CONF="/etc/rio"
    command=/www/rio/bin/rio start web
    autostart=true
    autorestart=true
    redirect_stderr=true
    stdout_logfile=syslog
    stderr_logfile=syslog

    [program:rio-worker]
    directory=/www/rio/
    environment=RIO_CONF="/etc/rio"
    command=/www/rio/bin/sentry start worker
    autostart=true
    autorestart=true
    redirect_stderr=true
    stdout_logfile=syslog
    stderr_logfile=syslog

Setup a Reverse Proxy
---------------------

You’ll use the builtin HttpProxyModule within Nginx to handle proxying::

    upstream rio_servers {
        server    127.0.0.1:9001;
    }

    server {
        listen 80;
        server_name rio.intra.yourcorp.com;

        location / {
            client_max_body_size 10M;
            proxy_redirect     off;
            proxy_set_header   Host             $host;
            proxy_set_header   X-Real-IP        $remote_addr;
            proxy_set_header   X-Forwarded-For  $proxy_add_x_forwarded_for;
            proxy_pass         http://rio_servers;
        }
    }

Removing Old Data
-----------------

One of the most important things you’re going to need to be aware of is storage costs.
The stale data in Backend storage should be automatically removed by a cron job::

    $ crontab -e
    0 0 * * * RIO_CONF=/etc/rio rio cleanup --days=30
