rio(WIP)
========

How to contribute?
-------------------

1. Start from scratch::

    $ virtualenv venv
    $ source venv/bin/activate
    (venv) $ python setup.py develop
    (venv) $ python -m rio db upgrade
    (venv) $ python -m rio runworker
    (venv) $ python -m rio runserver

2. Please use `gpg` to sign your commits.

How to Test?
--------------

Test::

    (venv) $ venv/bin/pip install -r tests-requirements.txt
    (venv) $ venv/bin/py.test tests
