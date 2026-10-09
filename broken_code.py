def calculate_sum(items):
    total = 0
    for item in items:
        if isinstance(item, (int, float)):
            total += item
        else:
            raise TypeError("All elements must be numbers")
    return total