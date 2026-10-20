def parse_status(lines: list[str]) -> str:
    values = [line[len("STATUS=") :].strip() for line in lines if line.startswith("STATUS=")]
    if not values:
        raise ValueError("missing status")
    return values[-1]
