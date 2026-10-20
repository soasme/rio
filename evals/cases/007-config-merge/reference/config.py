from copy import deepcopy


def merge(defaults: dict, overrides: dict) -> dict:
    result = deepcopy(defaults)
    for key, value in overrides.items():
        result[key] = (
            merge(result[key], value)
            if isinstance(result.get(key), dict) and isinstance(value, dict)
            else deepcopy(value)
        )
    return result
