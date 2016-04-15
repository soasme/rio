rio(WIP)
========

run::

    $ python manage.py db upgrade
    $ celery worker -A manage:celery
    $ python manage.py runserver
