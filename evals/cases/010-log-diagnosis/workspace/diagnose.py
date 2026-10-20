from parser import parse_status

for n in range(20000):
    print("diagnostic noise " + str(n))
assert parse_status(["STATUS=old", "STATUS=ready"]) == "ready"
