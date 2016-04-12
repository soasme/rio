# -*- coding: utf-8 -*-
"""
rio.exts.base
~~~~~~~~~~~~~

Base implementation of flask extension.
"""

# Find the stack on which we want to store the database connection.
# Starting with Flask 0.9, the _app_ctx_stack is the correct one,
# before that we need to use the _request_ctx_stack.
try:
    from flask import _app_ctx_stack as stack
except ImportError:
    from flask import _request_ctx_stack as stack

class Extension(object):
    """Base extension."""

    def __init__(self, app=None):
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_extension(self, app):
        """Any inherited extension should implement this method.

        Normally do some configuration initialization stuff here.

        Example::

            def init_extension(self, app):
                app.config.setdefault('RIO_XXX', True)

        :param app: Flask application.
        """
        raise NotImplementedError

    def init_app(self, app):
        """Initialize extension to the given application.

        Extension will be registered to `app.extensions` with lower classname
        as key and instance as value.

        :param app: Flask application.
        """
        self.init_extension(app)

        if not hasattr(app, 'extensions'):
            app.extensions = {}
        classname = self.__class__.__name__
        extname = classname.replace('Flask', '').lower()
        app.extensions[extname] = self

    def context(self, key, method):
        """A helper method to attach a value within context.

        :param key: the key attached to the context.
        :param method: the constructor function.
        :return: the value attached to the context.
        """
        ctx = stack.top
        if ctx is not None:
            if not hasattr(ctx, key):
                setattr(ctx, key, method())
            return getattr(ctx, key)
