# -*- coding: utf-8 -*-

class MissingSender(Exception):
    pass


class WrongSenderSecret(Exception):
    pass


class NotAllowed(Exception):
    pass


class MissingProject(Exception):
    pass

class MissingAction(Exception):
    pass
