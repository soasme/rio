import importlib
import json


def encode(value: object) -> str:
    try:
        fastjson = importlib.import_module("fastjson")
    except ModuleNotFoundError as exc:
        if exc.name != "fastjson":
            raise
        return json.dumps(value, sort_keys=True)
    return fastjson.dumps(value)
