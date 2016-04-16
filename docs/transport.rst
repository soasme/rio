Transport
==========

A transport is responsbile for collecting actions from services,
converting them to a Rio asynchronous task. This approach is
modular which allows for transports that acccept any type of data
from any producer comes with transports for HTTP, thrift, etc.
