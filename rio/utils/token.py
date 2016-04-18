# -*- coding: utf-8 -*-
"""
rio.utils.token
~~~~~~~~~~~~~~~
"""

import random
import string

def password_generator(length):
    """Generate a random password.

    :param length: integer.
    """
    return ''.join(random.choice(string.ascii_lowercase + string.digits)
                   for _ in range(length))
