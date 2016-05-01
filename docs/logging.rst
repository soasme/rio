Logging
=======

Sometimes you might want to dive into Rio to find out the data whether it is
right or wrong. Python's standard logging module is used to implement
informational and debug log output with Rio. You can integrate Rio's logging
in a standard way with other libraries and applications.

There are several loggers listed below that can be turned on:

* `rio.tasks` - controls asynchronous tasks running logging. set to
  `logging.INFO` for requesting webhook, `logging.DEBUG` for requesting
  webhook and receiving webhook's response, `logging.ERROR` for error
  response.
* `rio.event` - controls event emitting logging. set to `logging.INFO`
  for receiving action.

For example, you can writing logging configure codes in config file::

    import logging

    logger = logging.getLogger('rio.tasks')
    logger.addHandler(logging.FileHandler('/tmp/rio.log'))
    logger.setLevel(logging.DEBUG)

    logger = logging.getLogger('rio.event')
    logger.addHandler(logging.FileHandler('/tmp/rio.log'))
    logger.setLevel(logging.DEBUG)

Once an action was emitted, Rio would apply logging into your handlers::

    $ curl http://example:example@127.0.0.1:5000/event/example/emit/example -X POST
    {
      "event": {
        "uuid": "2df0b14b-07b9-42ab-9595-59a58829d505"
      },
      "message": "ok",
      "task": {
        "id": "f1c10766-b428-4ac7-ac0b-6bf2b4420d15"
      }
    }

    $ cat /tmp/rio.log
    EMIT 2df0b14b-07b9-42ab-9595-59a58829d505 "example" "example" {}
    REQUEST 2df0b14b-07b9-42ab-9595-59a58829d505 POST http://127.0.0.1:5000 {}
    RESPONSE 2df0b14b-07b9-42ab-9595-59a58829d505 POST http://127.0.0.1:5000 {"message": "OK"}
