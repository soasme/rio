try:
    import fastjson
except ImportError:
    fastjson = None

import json


def encode(value: object) -> str:
    if fastjson is not None:
        return fastjson.dumps(value)
    return json.dumps(value, sort_keys=True)
