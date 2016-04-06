rio(WIP)
========

run::

    $ python manage.py shell
    >>> from rio.core import db
    >>> db.create_all()
    $ celery worker -A manage:celery
    $ python manage.py runserver
