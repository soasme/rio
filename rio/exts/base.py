# -*- coding: utf-8 -*-

# Find the stack on which we want to store the database connection.
# Starting with Flask 0.9, the _app_ctx_stack is the correct one,
# before that we need to use the _request_ctx_stack.
try:
    from flask import _app_ctx_stack as stack
except ImportError:
    from flask import _request_ctx_stack as stack

class Extension(object):

    def __init__(self, app=None):
        self.app = app
        if app is not None:
            self.init_app(app)

    def init_extension(self, app):
        raise NotImplementedError

    def init_app(self, app):
        self.init_extension(app)

        if not hasattr(app, 'extensions'):
            app.extensions = {}
        classname = self.__class__.__name__
        extname = classname.replace('Flask', '').lower()
        app.extensions[extname] = self

    def context(self, key, method):
        ctx = stack.top
        if ctx is not None:
            if not hasattr(ctx, key):
                setattr(ctx, key, method())
            return getattr(ctx, key)
