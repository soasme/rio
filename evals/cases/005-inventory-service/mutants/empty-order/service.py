from inventory import reserve


def checkout(stock: dict, order: dict) -> dict:
    result = stock
    for sku, count in order.items():
        result = reserve(result, sku, count)
    return result
