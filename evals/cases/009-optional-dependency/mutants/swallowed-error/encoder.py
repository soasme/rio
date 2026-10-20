import json


def encode(value: object) -> str:
    try:
        import fastjson

        return fastjson.dumps(value)
    except ImportError:
        return json.dumps(value, sort_keys=True)
