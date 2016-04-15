# -*- coding: utf-8 -*-
"""
rio.utils.inject
~~~~~~~~~~~~~~~~
"""

import re

ENV_PLACEHODLER = re.compile(r'{{\s?(\w+)\s?}}')

def format_config(data, env):
    """
    Format data with given env.
    :param data: a string/integer/float/boolean/list/dict object.
    :param env: a dictionary.
    :return: return formatted data.
    `format_config` will try to format strings contained env placeholder `{{ ENV_KEY }}`.
    If `ENV_KEY` does not exist in env, then this function will trhow assertion error.
    """
    if isinstance(data, list):
        return [format_config(datum, env) for datum in data]
    elif isinstance(data, dict):
        return {
            format_config(key, env): format_config(value, env)
            for key, value in data.items()
        }
    elif isinstance(data, str):
        def replace(match):
            assert env.get(match.group(1))
            return env.get(match.group(1))
        return ENV_PLACEHODLER.sub(replace, data)
    else:
        return data
