.. _upgrading:

Upgrading
==========


Upgrading the Package
----------------------

    $ pip install -U rio

Running migrations
-------------------

    $ rio upgrade


Restarting Services
-------------------

    $ supervisorctl restart rio-web
    $ supervisorctl restart rio-worker
