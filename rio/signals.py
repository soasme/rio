# -*- coding: utf-8 -*-
"""
rio.signals
~~~~~~~~~~~
"""


from blinker import signal

event_received = signal('event-received')
webhook_ran = signal('webhook-ran')
