def reserve(stock: dict, sku: str, quantity: int) -> dict:
    if quantity <= 0 or quantity > stock[sku]:
        raise ValueError("invalid quantity")
    result = stock.copy()
    result[sku] -= quantity
    return result
