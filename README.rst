rio(WIP)
========

How to contribute?
-------------------

1. Start from scratch::

    $ virtualenv venv
    $ source venv/bin/activate
    (venv) $ python setup.py develop
    (venv) $ rio db upgrade
    (venv) $ rio runworker
    (venv) $ rio runserver

2. Please use `gpg` to sign your commits.

How to Test?
--------------

Test::

    (venv) $ pip install -r tests-requirements.txt
    (venv) $ venv/bin/py.test tests
