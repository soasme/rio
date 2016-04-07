.. _configurations:

Configurations
================

Must set configuration items
----------------------------

SECRET_KEY
``````````

the secret key.

DO NOT LEAK IT.

SQLALCHEMY_DATABASE_URI
````````````````````````

This item specifies the database.
See more at

CELERY_BROKER_URL
``````````````````

This item specifies the broker.
See http://celery.readthedocs.org/en/latest/configuration.html#broker-url

CELERY_RESULT_BACKEND
``````````````````````

This item specifies the result backend.
See http://celery.readthedocs.org/en/latest/configuration.html#database-backend-settings
